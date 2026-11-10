import pytest

from app.roles import guess_role
from engine.mb import parse_opening_tag, parse_room_label, parse_schedule_entry


@pytest.mark.parametrize("text, expected", [
    ("BEDROOM-1\n3600X3000", ("BEDROOM-1", 3600.0, 3000.0)),
    ("STORE- 2\n2400X1800", ("STORE-2", 2400.0, 1800.0)),
    ("CHILDREN'S ROOM\n3300X3600", ("CHILDREN'S ROOM", 3300.0, 3600.0)),
    ("MAINTEN-\nANCE \nROOM\n2700X3300", ("MAINTENANCE ROOM", 2700.0, 3300.0)),
    ("COMMON\nTOILET\n1800X2400", ("COMMON TOILET", 1800.0, 2400.0)),
    ("W.C.\n1200 x 1000", ("W.C.", 1200.0, 1000.0)),
    ("LIVING ROOM 4200 X 3600", ("LIVING ROOM", 4200.0, 3600.0)),
])
def test_room_label_parses(text, expected):
    assert parse_room_label(text) == expected


@pytest.mark.parametrize("text", [
    "450X450\nKHURRA",                       # size before name: not a room
    "D1 (1200X2100)",                         # opening schedule entry
    "600MM HIGH\nAND 230MM THK.\nPARAPET WALL",
    "LVL+150",
    "3600X3000",                              # no name
    "D1\n900X2100",                           # tag, not a room
])
def test_room_label_rejects(text):
    assert parse_room_label(text) is None


def test_opening_tags_and_schedule():
    assert parse_opening_tag("D1") == "D1"
    assert parse_opening_tag(" W2' ") == "W2'"
    assert parse_opening_tag("V1") == "V1"
    assert parse_opening_tag("WC") is None
    assert parse_opening_tag("DOOR") is None
    assert parse_schedule_entry("D3' (750X2100)") == ("D3'", 750.0, 2100.0)
    assert parse_schedule_entry("SD- (1800X2100)") == ("SD", 1800.0, 2100.0)
    assert parse_schedule_entry("BEDROOM-1") is None


@pytest.mark.parametrize("name, role", [
    ("A-WALL", "wall-brick"), ("A-WALL PARAPET", "parapet"), ("A-COLUMN", "column"),
    ("A-DW TAG", "door"), ("A-DIM GRID", "dimension"), ("A-DIM MAIN", "dimension"),
    ("A-WINDOW", "window"), ("A-TEXT DETAIL", "room text"), ("A-HATCH WALL", "ignore"),
    ("wall 200", "wall-AAC"), ("A-FURN", "ignore"), ("XREF$0$A-WALL", "wall-brick"),
])
def test_role_guess(name, role):
    assert guess_role(name) == role
