# Modbus Device Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and adversarially verify a read-only scanner that discovers TH/THC sensors, two labeled water probes, EC092 fans, and two ADL electricity meters across three CM5 RS-485 ports.

**Architecture:** `tools/modbus_profiles.py` owns Modbus frame validation and device-specific decoding, while `tools/modbus_sensor_scanner.py` owns the existing TCP bridge transport, staged scan orchestration, concurrency across independent ports, output, and CLI behavior. No profile can construct a write request. Verification combines deterministic unit tests, orchestration tests with a fake transport, independent review agents, and repeated clean rounds.

**Tech Stack:** Python 3 standard library, pytest, Lumina `lumina-modbus-server` line protocol.

## Global Constraints

- Use only Modbus functions `0x03` and `0x04`; never generate `0x05`, `0x06`, `0x0F`, or `0x10`.
- Default serial ports are `/dev/ttyAMA1`, `/dev/ttyAMA2`, and `/dev/ttyAMA3` at 9600 baud.
- Default discovery range is `0x01-0xF7`.
- Water probe `0x70` is "Tank 1 cooling/dehumidification coil return water".
- Water probe `0x80` is "Tank 1 reheat coil return water".
- Electricity-meter targets are decimal 147 (`0x93`) and 194 (`0xC2`).
- EC092 probes use function `0x04`, count 9, with PDU bases decimal 3001, decimal 3000, and `0x0000` in that order.
- Preserve raw response hex and distinguish timeout, exception, malformed, unknown, and found results.
- Scan separate serial ports concurrently but keep requests on each serial port sequential.
- Do not edit or deploy `device.conf`; print suggestions only.

---

### Task 1: Protocol Frames and Profile Decoders

**Files:**
- Create: `tools/modbus_profiles.py`
- Create: `tests/test_modbus_profiles.py`
- Modify: `tests/test_modbus_sensor_scanner.py`

**Interfaces:**
- Produces: `ProbeSpec(profile, function_code, start_register, count)`, `DecodedDevice(profile, model, values, raw_hex)`, `build_read_frame(slave_address, function_code, start_register, count)`, `decode_th_response(response, expected_address)`, `decode_ec092_response(response, expected_address)`, and `decode_adl_meter_response(response, expected_address)`.
- Consumes: no project code; Python `dataclasses`, `struct`, and typing only.

- [ ] **Step 1: Write failing frame and malformed-response tests**

Add tests that assert:

```python
assert build_read_frame(0x10, 0x03, 0, 11).hex() == "10030000000b074c"
assert build_read_frame(0x01, 0x04, 3001, 9).hex() == "01040bb90009e3cd"
with pytest.raises(ValueError, match="read function"):
    build_read_frame(1, 0x06, 0, 1)
```

Add parametrized cases for a bad CRC, wrong slave address, wrong function,
odd byte count, truncated payload, extra bytes, exception response, address
`0xF8`, count zero, and count 126. Each decoder must return a structured
failure or `None` rather than accepting the frame.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_modbus_profiles.py -v
```

Expected: collection or assertion failure because `tools/modbus_profiles.py`
and its interfaces do not exist.

- [ ] **Step 3: Implement the read-only frame boundary**

Implement:

```python
READ_FUNCTIONS = frozenset({0x03, 0x04})

def build_read_frame(slave_address: int, function_code: int,
                     start_register: int, count: int) -> bytes:
    if not 1 <= slave_address <= 0xF7:
        raise ValueError("slave_address must be between 0x01 and 0xF7")
    if function_code not in READ_FUNCTIONS:
        raise ValueError("function_code must be a read function (0x03 or 0x04)")
    if not 0 <= start_register <= 0xFFFF:
        raise ValueError("start_register must fit in uint16")
    if not 1 <= count <= 125:
        raise ValueError("count must be between 1 and 125")
    body = struct.pack(">BBHH", slave_address, function_code,
                       start_register, count)
    return body + modbus_crc(body)
```

The shared parser validates exact address, function, byte count, response
length, and CRC before returning register values. It recognizes five-byte
Modbus exception frames and exposes the exception code without decoding data.

- [ ] **Step 4: Run frame tests and verify GREEN**

Run the focused tests again. Expected: frame and validation tests pass.

- [ ] **Step 5: Write failing TH, EC092, and ADL decoder tests**

Cover these concrete values:

```python
# SHANHENG: 23.5 C, 55.5% RH, 800 ppm CO2
# WMS: swapped 25.3 C and 60.2% RH
# Water probes: valid TH frames at addresses 0x70 and 0x80
# EC092: 1450 rpm, no fault, no limit, 310 V, 1.23 A, 280 W, 42 C
# ADL: 230.1 V, 2.34 A, -0.125 kW, 0.950 PF, 50.00 Hz
```

Add adversarial cases for negative signed temperature, humidity `0`, humidity
over 100%, CO2 sentinel `0`, EC092 speed `65535`, heatsink temperature over
200 C, meter voltage over 600 V, power factor outside `-1.0..1.0`, and a valid
TH response offered to the meter decoder.

- [ ] **Step 6: Run decoder tests and verify RED**

Expected: failures for missing profile decoders and labels.

- [ ] **Step 7: Implement minimal decoders and plausibility checks**

Use profile-specific output dictionaries:

```python
DecodedDevice(
    profile="ec092",
    model="EC092",
    values={"speed_rpm": 1450, "fault_code": 0, "limit_code": 0,
            "bus_voltage_v": 310, "phase_current_a": 1.23,
            "power_w": 280, "heatsink_temperature_c": 42},
    raw_hex=response.hex(),
)
```

Keep zero speed and zero meter load valid because connected equipment may be
stopped or de-energized. Require the complete profile response and physically
bounded non-sentinel values to reduce false positives.

- [ ] **Step 8: Run all profile tests and verify GREEN**

Run:

```bash
python3 -m pytest tests/test_modbus_profiles.py tests/test_modbus_sensor_scanner.py -v
```

Expected: all protocol and legacy scanner tests pass.

### Task 2: Staged Scanner, CLI, and Output

**Files:**
- Modify: `tools/modbus_sensor_scanner.py`
- Modify: `tests/test_modbus_sensor_scanner.py`

**Interfaces:**
- Consumes: all public interfaces from `tools/modbus_profiles.py`.
- Produces: `ProbeRequest`, `ScanResult`, `build_scan_plan(...)`, `execute_scan_plan(...)`, table/JSON/config-suggestion output, and the documented CLI.

- [ ] **Step 1: Write failing scan-plan tests**

Assert that the default plan:

```python
plan = build_scan_plan(
    ports=["/dev/ttyAMA1", "/dev/ttyAMA2", "/dev/ttyAMA3"],
    baudrates=[9600],
    addresses=list(range(1, 0xF8)),
    profiles=["th", "ec092", "meter"],
)
assert plan[0].address == 0x01
assert {request.address for request in plan if request.profile == "meter"} == {147, 194}
assert all(request.function_code in {0x03, 0x04} for request in plan)
assert not any(request.function_code in {0x05, 0x06, 0x0F, 0x10} for request in plan)
```

Also assert known-address priority, no duplicate request keys, EC092 base order,
and that meters are never sprayed across unknown addresses.

- [ ] **Step 2: Run scan-plan tests and verify RED**

Expected: missing orchestration interfaces.

- [ ] **Step 3: Implement deterministic staged planning**

Build immutable requests in four stages: known EC092, known water probes, known
meters, then remaining TH and EC092 discovery. A request key is `(port,
baudrate, address, profile, function_code, start_register, count)`.

- [ ] **Step 4: Run scan-plan tests and verify GREEN**

Expected: plan ordering and read-only invariants pass.

- [ ] **Step 5: Write failing transport and fallback tests**

Use a callable fake transport that records requests and returns scripted raw
frames. Cover:

- TH two-register success followed by eleven-register enrichment.
- TH two-register timeout with no unnecessary enrichment call.
- EC092 bases 3001 timeout, 3000 malformed, and zero-base valid.
- Immediate stop after the first valid EC092 base.
- Meter response only at decimal 194.
- Same address on two ports retained as two devices.
- Duplicate response on one port deduplicated.
- One port raising `OSError` while the other two finish.
- Out-of-order concurrent port completion yielding deterministic sorted output.

- [ ] **Step 6: Run orchestration tests and verify RED**

Expected: executor and fallback behavior tests fail.

- [ ] **Step 7: Implement execution with one worker per port**

Use `ThreadPoolExecutor(max_workers=len(ports))`. Each worker executes its own
requests sequentially. Merge results after workers finish and sort by port,
baud, address, and profile. Do not retry malformed or CRC-invalid responses as
another device type unless the scan plan explicitly contains that profile.

- [ ] **Step 8: Run orchestration tests and verify GREEN**

Expected: all fake-transport tests pass without timing sleeps.

- [ ] **Step 9: Write failing CLI and rendering tests**

Test `--help`, `--profiles`, `--addresses`, `--fan-register-bases`,
`--include-misses`, `--json`, and invalid descending ranges. Assert decimal and
hex meter addresses, water labels, EC092 watchdog warning, raw hex in JSON, and
candidate config lines in text output.

- [ ] **Step 10: Implement CLI and output**

Defaults are:

```text
--ports /dev/ttyAMA1,/dev/ttyAMA2,/dev/ttyAMA3
--baudrates 9600
--addresses 0x01-0xF7
--profiles th,ec092,meter
--fan-register-bases 3001,3000,0
--timeout 0.7
```

Print progress to stderr so `--json` stdout remains valid JSON. Configuration
suggestions never write files.

- [ ] **Step 11: Run scanner and relay tests**

Run:

```bash
python3 -m pytest tests/test_modbus_profiles.py tests/test_modbus_sensor_scanner.py tests/test_relay_gui.py -v
```

Expected: all tests pass.

### Task 3: Multi-Round Adversarial Verification and Convergence

**Files:**
- Modify if findings require fixes: `tools/modbus_profiles.py`
- Modify if findings require fixes: `tools/modbus_sensor_scanner.py`
- Modify if findings expose gaps: `tests/test_modbus_profiles.py`
- Modify if findings expose gaps: `tests/test_modbus_sensor_scanner.py`
- Update: `docs/superpowers/plans/2026-07-11-modbus-device-scanner.md`

**Interfaces:**
- Consumes: completed Tasks 1 and 2 and their test evidence.
- Produces: a verification summary with at least two consecutive clean rounds.

- [ ] **Step 1: Verification round 1 - protocol and safety audit**

Dispatch independent reviewers for: (a) Modbus framing/CRC/exception handling,
(b) profile false positives and physical ranges, and (c) proof that no write
function can be emitted. Reviewers report only new Critical, Important, or
Minor findings with exact file and line references.

- [ ] **Step 2: Fix every Critical and Important round-1 finding with TDD**

For each valid finding, add a test that fails for the reported case, run it to
confirm RED, implement the minimal correction, then run the focused suite to
confirm GREEN. Record rejected findings with technical evidence.

- [ ] **Step 3: Verification round 2 - orchestration and hostile-input audit**

Dispatch independent reviewers for: (a) timeout/partial-response/disconnection
behavior, (b) concurrency and deterministic deduplication, and (c) CLI parsing,
JSON purity, output escaping, and pathological address lists.

- [ ] **Step 4: Fix every Critical and Important round-2 finding with TDD**

Repeat the RED-GREEN correction loop and record outcomes.

- [ ] **Step 5: Convergence rounds**

Run fresh review rounds against the full current diff. A round is clean only
when all reviewers report no new Critical or Important findings. Continue until
two consecutive rounds are clean; any valid new finding resets the clean-round
count to zero.

- [ ] **Step 6: Fresh final verification**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/modbus_scanner_pycache \
python3 -m py_compile tools/modbus_profiles.py tools/modbus_sensor_scanner.py
python3 -m pytest tests/test_modbus_profiles.py tests/test_modbus_sensor_scanner.py tests/test_relay_gui.py -v
python3 tools/modbus_sensor_scanner.py --help
```

Expected: compilation exits 0, pytest reports zero failures, and help lists all
three profiles and the read-only warning.

- [ ] **Step 7: Record the verification summary**

Append a table with round number, reviewer scope, findings, fixes, test evidence,
and clean-round status. Do not report completion until two consecutive clean
rounds and the fresh final commands all succeed.

## System-Level Summary

The scanner becomes a read-only protocol-aware discovery pipeline rather than
a TH-specific loop. Device profiles validate raw Modbus responses before data
enters orchestration; the scan planner decides where and in what order profiles
run; independent per-port workers execute those plans without concurrent access
to one RS-485 bus. This structure contains false positives, keeps write commands
unrepresentable, and lets deterministic tests reproduce disconnected devices,
partial frames, address collisions, and mixed hardware before the CM5 is used.
