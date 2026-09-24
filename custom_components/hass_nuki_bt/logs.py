"""Read optional Nuki logs without replaying a consumed challenge."""

import asyncio
import logging

from pyNukiBT import NukiDevice
from pyNukiBT.const import NukiErrorException

_LOGGER = logging.getLogger(__name__)


async def async_request_log_entries(
    device: NukiDevice, security_pin: int, *, count: int = 1, start_index: int = 0
):
    """Retry a read once, obtaining a fresh challenge for each attempt.

    pyNukiBT 0.0.20 retries the already encoded request after a response
    timeout. Its payload nonce may already have been consumed by the device.
    Keep the whole challenge/read transaction under its operation lock and
    disable response replay for each individual command instead.
    """
    const = device._const
    command = const.NukiCommand
    for attempt in range(2):
        try:
            async with device._operation_lock:
                challenge = await device._send_encrypted_command(
                    command.REQUEST_DATA,
                    {"command": command.CHALLENGE},
                    expected_response=command.CHALLENGE,
                    response_retry=1,
                )
                result = await device._send_encrypted_command(
                    command.REQUEST_LOG_ENTRIES,
                    {
                        "start_index": start_index,
                        "count": count,
                        "sort_order": 0x01,
                        "total_count": 0,
                        "nonce": challenge["nonce"],
                        "security_pin": security_pin,
                    },
                    aggregate_messages=[command.LOG_ENTRY],
                    expected_response=command.STATUS,
                    response_retry=1,
                )
                if result["status"] != const.StatusCode.COMPLETED:
                    raise RuntimeError("Nuki did not complete the log request")
                return list(device._messages)
        except NukiErrorException as err:
            if attempt or err.error_code != const.ErrorCode.K_ERROR_BAD_NONCE:
                raise
        except asyncio.TimeoutError:
            if attempt:
                raise
        _LOGGER.debug("Retrying Nuki log read with a fresh challenge")
