import json

from phonebot.core.models import Mode, RedFlag
from phonebot.core.settings import ProfitRule, SalesChannel, Settings


def test_roundtrip():
    s = Settings(
        mode="resell",
        profit_resell=ProfitRule(200, 25, "percent"),
        sales_channels=[SalesChannel("Allegro", 8, 1, 0)],
        active_sales_channel="Allegro",
        manual_market_values={"iPhone 13|128": 1500.0},
        watched_models=["iPhone 13"],
    )
    restored = Settings.from_json(s.to_json())
    assert restored == s
    assert restored.mode_enum is Mode.RESELL
    assert restored.profit_rule(Mode.RESELL).min_amount == 200
    assert restored.sales_channel().commission_pct == 8


def test_missing_and_unknown_keys():
    data = {"refresh_minutes": 5, "nieznany_klucz": 1, "profit_repair": {"min_amount": 99}}
    s = Settings.from_json(json.dumps(data))
    assert s.refresh_minutes == 5
    assert s.profit_repair.min_amount == 99
    assert s.profit_repair.min_percent == 20  # domyślne
    assert s.penalty(RedFlag.ICLOUD_LOCK) == 60


def test_broken_json_gives_defaults():
    assert Settings.from_json("{nie json") == Settings()
    assert Settings.from_json(None) == Settings()


def test_new_flag_penalties_are_added_to_old_settings():
    s = Settings.from_json(json.dumps({"flag_penalties": {"icloud_lock": 99}}))
    assert s.penalty(RedFlag.ICLOUD_LOCK) == 99
    assert s.penalty(RedFlag.NO_PHOTOS) == 15
