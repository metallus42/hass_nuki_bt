"""Change the Opener pairing permission using its existing authorization."""

from pyNukiBT import NukiConst, NukiDevice, NukiOpenerConst


async def async_set_opener_pairing_enabled(
    device: NukiDevice, security_pin: int | None, enabled: bool
) -> bool:
    """Read, modify and verify basic config without changing other settings.

    pyNukiBT 0.0.20 defines the Opener NewConfig payload but does not register
    SET_CONFIG. Keep this compatibility shim confined to the Opener protocol.
    Its operation lock prevents polling from consuming a config challenge.
    """
    if device.device_type != NukiConst.NukiDeviceType.OPENER:
        raise ValueError("Bluetooth pairing configuration is only supported for Openers.")
    if security_pin is None:
        raise ValueError("A security PIN is required to change Bluetooth pairing.")
    if type(security_pin) is not int or not 0 <= security_pin <= 65535:
        raise ValueError("The Opener security PIN must be an unsigned 16-bit integer.")
    if type(enabled) is not bool:
        raise ValueError("The Bluetooth pairing permission must be a boolean.")

    const = NukiOpenerConst
    command = const.NukiCommand

    async def challenge():
        return await device._send_encrypted_command(
            command.REQUEST_DATA,
            {"command": command.CHALLENGE},
            expected_response=command.CHALLENGE,
        )

    async def read_config():
        nonce = await challenge()
        config = await device._send_encrypted_command(
            command.REQUEST_CONFIG,
            {"nonce": nonce["nonce"]},
            expected_response=command.CONFIG,
        )
        device.config = config
        device._poll_needed_config = False
        return config

    async with device._operation_lock:
        before = await read_config()
        if before["pairing_enabled"] == int(enabled):
            return enabled

        fields = [
            field.name
            for field in const.NewConfig.subcons
            if field.name not in ("nonce", "security_pin")
        ]
        # Use the freshly read values; never defaults or an old config cache.
        payload = {name: before[name] for name in fields}
        payload["pairing_enabled"] = int(enabled)
        nonce = await challenge()
        payload.update(nonce=nonce["nonce"], security_pin=security_pin)
        const.message_types.setdefault(command.SET_CONFIG, const.NewConfig)
        device._poll_needed_config = True
        result = await device._send_encrypted_command(
            command.SET_CONFIG,
            payload,
            expected_response=command.STATUS,
            response_retry=1,
        )
        if result["status"] != const.StatusCode.COMPLETED:
            raise RuntimeError("The Opener did not confirm the configuration change.")

        after = await read_config()
        if after["pairing_enabled"] != int(enabled):
            raise RuntimeError("The Opener did not retain the Bluetooth pairing permission.")
        if any(after[name] != before[name] for name in fields if name != "pairing_enabled"):
            raise RuntimeError("Another Opener configuration value changed during verification.")
        return enabled
