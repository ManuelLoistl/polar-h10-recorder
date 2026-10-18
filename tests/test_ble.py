from polar_recorder.ble import parse_hr


def test_parse_hr_uint8_with_rr_and_contact():
    # flags: RR present (0x10) | contact supported (0x04) | contact detected (0x02)
    data = bytes([0x16, 62, 0x00, 0x04, 0x33, 0x03])  # RR 1024 -> 1000 ms, 819 -> 799.8 ms
    bpm, rr, contact = parse_hr(data)
    assert bpm == 62
    assert contact == 1
    assert rr == [1000.0, 819 / 1024 * 1000]


def test_parse_hr_uint16_energy_no_contact():
    # flags: uint16 HR (0x01) | energy expended (0x08) | RR (0x10)
    data = bytes([0x19, 0x2C, 0x01, 0x10, 0x00, 0x00, 0x04])
    bpm, rr, contact = parse_hr(data)
    assert bpm == 300
    assert contact is None
    assert rr == [1000.0]


def test_parse_hr_contact_supported_not_detected():
    assert parse_hr(bytes([0x04, 70])) == (70, [], 0)
