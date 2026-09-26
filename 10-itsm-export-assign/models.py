from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class EventType(str, Enum):
    UNASSIGNED = "unassigned"
    ASSIGNED = "assigned"


class PersonEvent(BaseModel):
    event_type: EventType
    person_name: str
    timestamp: datetime | None = None
    performed_by: str = ""
    raw_text: str = ""


class BrowserConfig(BaseModel):
    user_data_dir: str = ""  # empty = the default Edge/Chrome user data dir
    profile_name: str = "Default"
    channel: str = "msedge"
    headless: bool = False


class PortalConfig(BaseModel):
    base_url: str = "https://portal.example.com/"  # PORTAL_BASE_URL (env) overrides
    asset_type_id: str = "ASSET-TYPE-ID"  # data-testid suffix of the asset type option
    asset_id: str = "ASSET-ID"  # data-testid suffix of the asset link
    asset_search_term: str = "Example Asset"
    max_load_more_clicks: int = 100
    load_more_wait_ms: int = 1500


class ExportFilter(BaseModel):
    event_type: EventType = EventType.UNASSIGNED
    filter_date: datetime | None = None


class AppConfig(BaseModel):
    """Config of the export bot (main.py)."""

    browser: BrowserConfig = BrowserConfig()
    portal: PortalConfig = PortalConfig()
    filter: ExportFilter = ExportFilter()
    output_file: str = "export_unassigned.xlsx"


class AssignConfig(BaseModel):
    """Config of the companion re-assign bot (reassign_people/main.py)."""

    browser: BrowserConfig = BrowserConfig()
    portal: PortalConfig = PortalConfig()
    input_file: str = "../export_unassigned.xlsx"
    assignment_delay_ms: int = 2000
