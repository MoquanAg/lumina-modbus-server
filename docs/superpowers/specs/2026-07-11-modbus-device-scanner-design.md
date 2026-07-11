# Modbus Device Scanner Design

## Purpose

Extend `tools/modbus_sensor_scanner.py` into a read-only discovery tool for the
three CM5 RS-485 ports. The scanner must locate and identify the connected
temperature/humidity sensors, two known water-temperature probes, EC092 reheat
coil fans, and two known electricity meters without changing device state.

## Supported Devices

### TH and THC Sensors

- Probe Modbus holding registers with function `0x03`.
- Read registers `0x0000-0x0001` first to discover SHANHENG or WMS temperature
  and humidity layouts at any valid slave address.
- After a successful two-register read, attempt the existing eleven-register
  read to detect CO2 at register `0x000A` and classify the device as THC.
- Scan slave addresses `0x01-0xF7` by default because the TH addresses are not
  known.
- Preserve physical-range validation so unrelated Modbus devices are not
  classified as environmental sensors merely because they respond.

### Water-Temperature Probes

The water probes use the legacy TH sensor register layout and are therefore
decoded by the same profile. Their known addresses receive fixed labels:

- `0x70`: Tank 1 cooling/dehumidification coil return water
- `0x80`: Tank 1 reheat coil return water

The scanner reports both temperature and humidity fields when the device
provides them, while presenting these two devices as water-temperature probes.

### EC092 Reheat Coil Fans

The EC092 protocol uses Modbus RTU at `9600 8N1` and defaults to slave address
`0x01`. Identification is read-only and uses function `0x04` for the documented
input-register block beginning at register 3001.

The decoder reports:

- Actual speed in RPM
- Fault code
- Frequency-limit code
- DC bus voltage
- Phase current
- Power
- IPM/heatsink temperature

The document does not show a wire-level example that resolves whether its
printed register number 3001 is encoded as 3001 or as a zero-based offset.
The profile therefore tries PDU start addresses decimal `3001`, decimal `3000`,
and `0x0000`, in that order, but stops after the first CRC-valid, physically
plausible response.

The scanner searches the full slave range for EC092 devices because there may
be multiple fans with reassigned addresses. A warning explains that the fan
stops after 120 seconds without valid communication; scanner reads can refresh
that watchdog even though the scanner never writes a speed command.

### Electricity Meters

Probe only the two known addresses, both at 9600 baud:

- Decimal 147 (`0x93`)
- Decimal 194 (`0xC2`)

Identification uses function `0x03` to read the existing ADL real-time block at
`0x000B-0x0011`. The scanner decodes voltage, current, active/reactive/apparent
power, power factor, and frequency. Plausibility checks reject responses that
do not resemble an electricity meter.

## Scan Strategy

The default scan is staged per serial port:

1. Probe known addresses first: EC092 at `0x01`, water probes at `0x70` and
   `0x80`, and electricity meters at `0x93` and `0xC2`.
2. Search all remaining addresses for TH/THC devices.
3. Search all remaining addresses for EC092 fans.
4. Deduplicate devices by port, baud rate, slave address, and profile.

Ports may be scanned concurrently because they are independent buses. Requests
on one port remain sequential to avoid collisions. Command-line options allow
operators to narrow ports, addresses, profiles, baud rates, and timeouts for a
faster follow-up scan.

## Transport and Safety

- Use the existing `lumina-modbus-server` TCP command protocol at
  `127.0.0.1:8888`.
- Use only Modbus functions `0x03` and `0x04`.
- Never issue functions `0x05`, `0x06`, `0x0F`, or `0x10`.
- Validate slave address, function code, byte count, response length, and CRC
  before decoding.
- Preserve raw response hex in JSON output for later diagnosis.
- Distinguish timeout, Modbus exception, malformed response, unknown response,
  and validated device results.

## Output

The default output is a concise tab-separated table containing profile, port,
baud rate, address in decimal and hexadecimal, label, and decoded readings.
`--json` emits the same information as structured records. A configuration
suggestion section prints candidate `device.conf` entries without editing any
configuration file.

Progress output includes the current stage and completed/total probes so a full
scan does not look stalled. Misses are hidden unless `--include-misses` is used.

## Testing

Tests cover:

- Function `0x03` and `0x04` request frames and CRCs
- SHANHENG TH/THC and WMS decoding
- Water-probe labels at `0x70` and `0x80`
- EC092 decoding for documented and zero-based register interpretations
- EC092 fault codes and signed/implausible values
- ADL electricity-meter decoding at decimal addresses 147 and 194
- CRC failures, wrong slave addresses, Modbus exception frames, truncated
  responses, and plausible responses from the wrong profile
- Scan ordering, deduplication, and the guarantee that no write function is
  generated

The implementation is complete when the focused scanner tests and the existing
relay GUI tests pass, Python compilation succeeds, and `--help` describes the
new profiles and safety behavior.
