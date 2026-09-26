"""
Export the current permission tree of one or more users from the portal to Excel
(scraping counterpart of permission_bot.py). Users and the user-tree path come
from export_config.yaml.
"""
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pyotp
import yaml
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from playwright.sync_api import Locator, Page, sync_playwright

import labels as ui

# =============================================================================
# Configuration from environment variables (loaded from .env if present)
# =============================================================================

# Load .env file from the project root
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)


def get_config():
    """
    Load configuration from environment variables.

    Required (browser login):
        PORTAL_USER: Regular user username for browser login
        PORTAL_PASS: Regular user password
        PORTAL_TOTP_SECRET: Base32 TOTP secret for automated 2FA

    Optional:
        HEADLESS: "true" (default) or "false" for headed mode
        LOG_DIR: Directory for log files (default: script directory)
        EXPORT_LOGIN_URL: SSO login page (default: $PORTAL_BASE_URL/account/login)

    Returns:
        dict with configuration values

    Raises:
        SystemExit if required variables are missing
    """
    missing = []

    user = os.environ.get("PORTAL_USER")
    if not user:
        missing.append("PORTAL_USER")

    password = os.environ.get("PORTAL_PASS", "").strip()
    if not password:
        missing.append("PORTAL_PASS")

    # Strip spaces from secret since authenticator apps often display with spaces
    totp_secret = os.environ.get("PORTAL_TOTP_SECRET", "").strip().replace(" ", "")
    if not totp_secret:
        missing.append("PORTAL_TOTP_SECRET")

    if missing:
        print(f"ERROR: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        print("\nRequired environment variables:", file=sys.stderr)
        print("  PORTAL_USER            - Regular user username for browser login", file=sys.stderr)
        print("  PORTAL_PASS            - Regular user password", file=sys.stderr)
        print("  PORTAL_TOTP_SECRET     - Base32 TOTP secret for 2FA", file=sys.stderr)
        print("\nOptional environment variables:", file=sys.stderr)
        print("  HEADLESS               - 'true' (default) or 'false'", file=sys.stderr)
        print("  LOG_DIR                - Directory for log files", file=sys.stderr)
        sys.exit(1)

    # Parse headless setting (default: True for production/scheduled runs)
    headless_str = os.environ.get("HEADLESS", "true").lower()
    headless = headless_str not in ("false", "0", "no")

    # Log directory (default: script directory)
    script_dir = Path(__file__).parent.resolve()
    log_dir = os.environ.get("LOG_DIR")
    if log_dir:
        log_dir = Path(log_dir).resolve()
    else:
        log_dir = script_dir

    # Ensure log directory exists
    log_dir.mkdir(parents=True, exist_ok=True)

    return {
        "user": user,
        "password": password,
        "totp_secret": totp_secret,
        "headless": headless,
        "log_dir": log_dir,
    }


# Load configuration early (before logging setup)
CONFIG = get_config()

# Configure logging with absolute path
log_filename = CONFIG["log_dir"] / f"permissions_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_filename, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# =============================================================================
# YAML export configuration
# =============================================================================

def load_export_config(filepath: str = None) -> dict:
    """
    Load export configuration from YAML file.

    Args:
        filepath: Path to YAML config file. Defaults to export_config.yaml

    Returns:
        Dict with keys: user_group (list[str]), users (list[str]), output (str)

    Raises:
        SystemExit if file not found or validation fails
    """
    if filepath is None:
        filepath = Path(__file__).parent / "export_config.yaml"
    else:
        filepath = Path(filepath)

    if not filepath.exists():
        logger.error(f"Export config not found: {filepath}")
        sys.exit(1)

    with open(filepath, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Validate required keys
    if not config or not isinstance(config, dict):
        logger.error("Export config is empty or invalid")
        sys.exit(1)

    user_group = config.get("user_group")
    if not user_group or not isinstance(user_group, list):
        logger.error("Export config missing 'user_group' (list of tree node names)")
        sys.exit(1)

    users = config.get("users")
    if not users or not isinstance(users, list):
        logger.error("Export config missing 'users' (list of user names)")
        sys.exit(1)

    output = config.get("output", "permissions_export.xlsx")

    logger.info(f"Export config loaded: {len(users)} user(s), output={output}")
    logger.info(f"User group path: {' > '.join(user_group)}")

    return {
        "user_group": user_group,
        "users": users,
        "output": output,
    }


# =============================================================================
# Browser login and navigation (same as permission_bot.py)
# =============================================================================

def setup_browser_and_navigate(page: Page, user_group: list[str]) -> Page:
    """
    Login via browser, handle 2FA with pyotp, navigate to user management.

    Args:
        page: Playwright page object
        user_group: List of tree node names to expand (from YAML config)

    Returns:
        Page object for admin popup (page1) after navigation setup

    Raises:
        RuntimeError: If login fails or dashboard doesn't load
    """
    # 1. Navigate to login page
    base_url = (os.environ.get("PORTAL_BASE_URL") or "https://portal.example.com").rstrip("/")
    login_url = os.environ.get("EXPORT_LOGIN_URL") or f"{base_url}/account/login"
    logger.info(f"Navigating to login page: {login_url}")
    page.goto(login_url)
    page.wait_for_load_state("networkidle")

    # 2. Fill credentials
    logger.info(f"Entering username: {CONFIG['user']}")
    page.locator(ui.LOGIN_USERNAME).fill(CONFIG["user"])
    page.locator(ui.LOGIN_PASSWORD).fill(CONFIG["password"])
    logger.info("Clicking login button")
    page.locator(ui.LOGIN_SUBMIT).click()
    page.wait_for_load_state("networkidle")

    # 3. Handle 2FA - automated via pyotp
    code_input = page.locator(ui.LOGIN_OTP_CODE)
    try:
        code_input.wait_for(state="visible", timeout=5000)
        totp_code = pyotp.TOTP(CONFIG["totp_secret"]).now()
        logger.info("Filling 2FA code (auto-generated via pyotp)")
        code_input.fill(totp_code)
        page.locator(ui.LOGIN_SUBMIT).click()
        page.wait_for_load_state("networkidle")
    except Exception:
        logger.info("No 2FA required for this account, continuing to dashboard")

    # 4. Wait for dashboard - look for admin button
    logger.info("Waiting for dashboard to load (looking for admin button)...")
    admin_link = page.locator(ui.ADMIN_LINK)
    admin_link.wait_for(state="visible", timeout=60000)
    logger.info("Dashboard loaded - admin button found")

    # 5. Open admin popup
    logger.info("Opening admin popup")
    with page.expect_popup() as page1_info:
        admin_link.click()
    page1 = page1_info.value
    page1.wait_for_load_state("domcontentloaded")
    logger.info("admin popup opened")

    return _navigate_to_user_management(page1, user_group)


def _navigate_to_user_management(page1: Page, user_group: list[str]) -> Page:
    """
    Navigate from admin to user management and expand the user tree.

    Args:
        page1: Playwright page object for the admin interface
        user_group: List of tree node names to expand (from YAML config)

    Returns:
        Same page1 object after navigation is complete
    """
    # Navigate to user management
    logger.info("Navigating to Users")
    page1.get_by_role("button", name=ui.BTN_USERS).locator("a").click()
    page1.wait_for_load_state("networkidle")

    logger.info("Navigating to user management")
    page1.get_by_role("button", name=ui.BTN_USER_MANAGEMENT).locator("a").click()
    page1.wait_for_load_state("networkidle")

    # Expand user tree using configurable path from YAML
    for node_name in user_group:
        expand_tree_node(page1, node_name)

    logger.info("Browser setup and navigation complete")
    return page1


# =============================================================================
# Tree navigation helpers (same as permission_bot.py)
# =============================================================================

def select_user_in_tree(page: Page, user_name: str):
    """Select a user in the tree view, scrolling if necessary."""
    logger.info(f"Looking for user: {user_name}")

    # Target the tree-node div with the matching title
    # Title format comes from labels.TREE_NODE_TITLE
    title_with_space = ui.TREE_NODE_TITLE.format(name=user_name)
    user_locator = page.locator(f'{ui.TREE_NODE}[title="{title_with_space}"]')

    # Wait for the element to exist in DOM
    user_locator.wait_for(state="attached", timeout=10000)

    # Scroll the element into view
    user_locator.scroll_into_view_if_needed()
    page.wait_for_timeout(200)  # Brief pause after scroll

    # Click on the user
    user_locator.click()
    logger.info(f"Selected user: {user_name}")


def is_node_expanded(node: Locator) -> bool:
    """
    Check if a tree node is currently expanded.

    Returns:
        False if node is collapsed or is a leaf node (no aria-expanded attribute)
        True if node is expanded
    """
    expanded_attr = node.get_attribute("aria-expanded")
    if expanded_attr is None:
        return False
    return expanded_attr == "true"


def expand_tree_node(page: Page, node_name: str, parent_name: str = None) -> None:
    """
    Expand a tree node by name, handling already-expanded state (idempotent).
    """
    logger.info(f"Finding node '{node_name}'")

    # Try two approaches to find the node:
    # 1. For user nodes: the tree-node div matched by its title attribute
    # 2. For group nodes: use get_by_role with exact name matching
    title_with_space = ui.TREE_NODE_TITLE.format(name=node_name)
    user_node_div = page.locator(f'{ui.TREE_NODE}[title="{title_with_space}"]')

    if user_node_div.count() > 0:
        logger.info(f"Found user node '{node_name}'")
        node = user_node_div.locator("xpath=ancestor::li[@role='treeitem'][1]")
    else:
        logger.info(f"Looking for group node '{node_name}'")
        node = page.get_by_role("treeitem", name=node_name, exact=False).first

    # Wait for node to exist in DOM
    node.wait_for(state="attached", timeout=10000)

    # Find all collapsed ancestor treeitems and expand them (top-down)
    collapsed_ancestors = node.locator("xpath=ancestor::li[@role='treeitem'][@aria-expanded='false']")
    ancestor_count = collapsed_ancestors.count()

    if ancestor_count > 0:
        logger.info(f"Found {ancestor_count} collapsed ancestor(s), expanding...")
        for i in range(ancestor_count):
            ancestor = collapsed_ancestors.nth(i)
            ancestor.locator("i").first.click()
            page.wait_for_timeout(200)

    # Check if target node is already expanded
    if is_node_expanded(node):
        logger.info(f"Node '{node_name}' already expanded, skipping")
        return

    # Expand the target node
    logger.info(f"Expanding node '{node_name}'")
    node.locator("i").first.click()

    # Wait for expansion to complete
    first_child = node.locator("[role='treeitem']").first
    first_child.wait_for(state="visible", timeout=5000)
    logger.info(f"Node '{node_name}' expanded successfully")


# =============================================================================
# Access editor open/close
# =============================================================================

def open_access_editor(page: Page, user_name: str) -> None:
    """
    Select user, open the permissions tab, open the access editor.

    Args:
        page: admin popup page
        user_name: User to select in the tree
    """
    select_user_in_tree(page, user_name)
    page.wait_for_timeout(500)

    # Open the permissions tab
    page.get_by_text(ui.TAB_PERMISSIONS, exact=True).click()
    page.wait_for_timeout(500)

    page.get_by_role("button", name=ui.BTN_MANAGE_ACCESS).locator("a").click()
    page.wait_for_load_state("networkidle")

    # Wait for the tree to load
    logger.info("Waiting for access-editor tree to load...")
    page.locator("[role='treeitem']").first.wait_for(state="visible", timeout=10000)
    page.wait_for_timeout(500)  # settle time
    logger.info("Access editor opened")


def close_access_editor(page: Page) -> None:
    """
    Close the access editor without saving.

    Tries: Close button -> tab X button -> Save -> Ok fallback.
    """
    # Strategy 1: Look for a labelled Close button
    close_label_btn = page.get_by_role("button", name=ui.BTN_CLOSE)
    if close_label_btn.count() > 0 and close_label_btn.first.is_visible():
        logger.info("Closing dialog via Close button")
        close_label_btn.first.click()
        page.wait_for_timeout(500)
        return

    # Strategy 2: Look for a close/X button on the dialog
    close_btn = page.locator(ui.DIALOG_CLOSE_X)
    if close_btn.count() > 0 and close_btn.first.is_visible():
        logger.info("Closing dialog via X button")
        close_btn.first.click()
        page.wait_for_timeout(500)
        return

    # Strategy 3: Save -> Ok (safe fallback - no changes were made)
    logger.info("Closing dialog via Save -> Ok (no changes made)")
    save_btn = page.get_by_role("button", name=ui.BTN_SAVE)
    if save_btn.count() > 0:
        save_btn.locator("a").click()
        page.wait_for_timeout(1000)
        ok_btn = page.get_by_role("button", name=ui.BTN_OK)
        if ok_btn.count() > 0:
            ok_btn.locator("a").click()
            page.wait_for_timeout(500)
            return

    logger.warning("Could not find a way to close the access editor")


# =============================================================================
# Recursive tree walker (THE CORE)
# =============================================================================

def get_node_name(node: Locator) -> str:
    """
    Extract the display name of a tree node.

    Tries multiple strategies:
      1. aria-label attribute
      2. title attribute on inner div (with trailing space stripped)
      3. First text content from span/a child

    Args:
        node: Locator for a treeitem element

    Returns:
        Node name string, or "unknown" if extraction fails
    """
    # Strategy 1: aria-label
    try:
        aria_label = node.get_attribute("aria-label")
        if aria_label and aria_label.strip():
            return aria_label.strip()
    except Exception:
        pass

    # Strategy 2: title attribute on inner div (user nodes have trailing space)
    try:
        inner_div = node.locator(ui.TREE_NODE).first
        if inner_div.count() > 0:
            title = inner_div.get_attribute("title")
            if title and title.strip():
                return title.strip()
    except Exception:
        pass

    # Strategy 3: First span or anchor text
    try:
        text_el = node.locator("> div span, > div a, > a, > span").first
        if text_el.count() > 0:
            text = text_el.text_content()
            if text and text.strip():
                return text.strip()
    except Exception:
        pass

    # Last resort: any text content (take first line, truncate)
    try:
        full_text = node.text_content()
        if full_text:
            first_line = full_text.strip().split("\n")[0].strip()
            if first_line:
                return first_line[:80]
    except Exception:
        pass

    return "unknown"


def get_checkbox_status(node: Locator) -> str:
    """
    Read the checkbox state of a tree node.

    Returns one of: "checked", "unchecked", "indeterminate", "no_checkbox"
    """
    # Try to find checkbox input
    checkbox = node.locator("input[type='checkbox']").first
    if checkbox.count() == 0:
        checkbox = node.get_by_role("checkbox").first
    if checkbox.count() == 0:
        return "no_checkbox"

    try:
        # Check for indeterminate state (tri-state checkbox)
        indeterminate = checkbox.evaluate("el => el.indeterminate")
        if indeterminate:
            return "indeterminate"

        if checkbox.is_checked():
            return "checked"
        else:
            return "unchecked"
    except Exception:
        return "no_checkbox"


def walk_tree_node(page: Page, node: Locator, node_name: str,
                   parent_path_parts: list[str], results: list[dict],
                   account_name: str, depth: int = 0) -> None:
    """
    Recursively walk a tree node and all its children, recording checkbox states.

    Args:
        page: Playwright page object
        node: Locator for the current treeitem
        node_name: Display name of the current node
        parent_path_parts: List of ancestor node names (for building ParentPath)
        results: List to append result dicts to (mutated in place)
        account_name: User account name (for the AccountName column)
        depth: Current recursion depth (for logging indentation)
    """
    indent = "  " * depth
    status = get_checkbox_status(node)

    # Build parent path string
    parent_path = " > ".join(parent_path_parts) if parent_path_parts else ""

    # Record this node
    results.append({
        "account": account_name,
        "parent_path": parent_path,
        "name": node_name,
        "status": status,
    })
    logger.info(f"{indent}[{status}] {node_name}")

    # Check if this node can be expanded (has aria-expanded attribute)
    expanded_attr = node.get_attribute("aria-expanded")
    if expanded_attr is None:
        # Leaf node, no children to recurse into
        return

    # Expand if collapsed
    if expanded_attr == "false":
        try:
            expand_icon = node.locator("> div i, > i").first
            if expand_icon.count() > 0 and expand_icon.is_visible():
                expand_icon.click()
                # Wait for children to appear
                first_child = node.locator("> ul > li[role='treeitem']").first
                first_child.wait_for(state="visible", timeout=5000)
                page.wait_for_timeout(200)  # settle time
                logger.info(f"{indent}  Expanded '{node_name}'")
            else:
                logger.warning(f"{indent}  Cannot expand '{node_name}': expand icon not found/visible")
                return
        except Exception as e:
            logger.warning(f"{indent}  Failed to expand '{node_name}': {e}")
            return

    # Get direct children: > ul > li[role='treeitem']
    children = node.locator("> ul > li[role='treeitem']").all()

    if not children:
        # Fallback: try broader child selector
        children = node.locator("ul > li[role='treeitem']").all()

    logger.info(f"{indent}  Found {len(children)} children under '{node_name}'")

    # Build path for children
    child_path_parts = parent_path_parts + [node_name]

    for child in children:
        try:
            child_name = get_node_name(child)
            walk_tree_node(page, child, child_name, child_path_parts,
                           results, account_name, depth + 1)
        except Exception as e:
            logger.error(f"{indent}  Error processing child of '{node_name}': {e}")


def export_user_permissions(page: Page, user_name: str) -> list[dict]:
    """
    Export all permission states for a single user.

    Args:
        page: admin popup page (already at user management)
        user_name: User to export permissions for

    Returns:
        List of permission result dicts
    """
    logger.info(f"{'=' * 60}")
    logger.info(f"Exporting permissions for: {user_name}")
    logger.info(f"{'=' * 60}")

    results = []

    try:
        # Open the access editor for this user
        open_access_editor(page, user_name)

        # Get all root-level tree items
        root_items = page.locator("[role='tree'] > li[role='treeitem']").all()

        if not root_items:
            # Fallback: try broader selector
            root_items = page.locator("[role='treeitem']").all()
            # Filter to only top-level items (those without treeitem ancestors)
            # This is a heuristic - just take all visible items
            logger.info(f"Using fallback selector, found {len(root_items)} tree items")

        logger.info(f"Found {len(root_items)} root tree nodes")

        for root_item in root_items:
            try:
                root_name = get_node_name(root_item)
                walk_tree_node(page, root_item, root_name, [], results,
                               user_name, depth=0)
            except Exception as e:
                logger.error(f"Error processing root node: {e}")

        logger.info(f"Collected {len(results)} permission entries for {user_name}")

    except Exception as e:
        logger.error(f"Failed to export permissions for {user_name}: {e}")

    finally:
        # Close the dialog
        close_access_editor(page)

    return results


# =============================================================================
# Excel output
# =============================================================================

def write_excel(results: list[dict], output_path: str) -> None:
    """
    Write permission results to an Excel file.

    Args:
        results: List of dicts with keys: account, parent_path, name, status
        output_path: Path to write the Excel file

    Output columns:
        AccountName | ParentPath | PermissionName | Status
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Permissions Export"

    # Header row
    headers = ["AccountName", "ParentPath", "PermissionName", "Status"]
    ws.append(headers)

    # Style headers
    header_font = Font(bold=True)
    header_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    for col_idx, _header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill

    # Data rows
    for entry in results:
        ws.append([
            entry["account"],
            entry["parent_path"],
            entry["name"],
            entry["status"],
        ])

    # Auto-fit column widths (approximate)
    for col_idx, header in enumerate(headers, start=1):
        max_len = len(header)
        for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
            for cell in row:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = min(max_len + 2, 80)

    # Auto-filter
    ws.auto_filter.ref = ws.dimensions

    # Ensure output directory exists
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    wb.save(output_path)
    logger.info(f"Excel file saved: {output_path} ({len(results)} rows)")


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    """
    Main entry point for permission export.

    Returns:
        0 on success
        1 on failure
    """
    logger.info("Starting permission export script")
    logger.info(f"Configuration: headless={CONFIG['headless']}, log_dir={CONFIG['log_dir']}")
    logger.info(f"Authentication: browser login with user={CONFIG['user']}")

    # Load export configuration from YAML
    export_config = load_export_config()

    all_results = []
    with sync_playwright() as p:
        logger.info(f"Launching Chromium browser (headless={CONFIG['headless']})")
        browser = p.chromium.launch(headless=CONFIG["headless"])
        context = browser.new_context()
        page = context.new_page()

        try:
            # Login, navigate to user management, expand user tree
            page1 = setup_browser_and_navigate(page, export_config["user_group"])

            # Export each user's permissions
            for idx, user_name in enumerate(export_config["users"], start=1):
                logger.info(f"User {idx}/{len(export_config['users'])}: {user_name}")
                user_results = export_user_permissions(page1, user_name)
                all_results.extend(user_results)

        except Exception as e:
            logger.error(f"Script failed with error: {e}")
            return 1
        finally:
            logger.info("Closing browser")
            context.close()
            browser.close()

    # Write results to Excel
    if all_results:
        write_excel(all_results, export_config["output"])
        logger.info(f"Export complete: {len(all_results)} permissions exported")
    else:
        logger.warning("No permissions were collected - Excel file not created")
        return 1

    # Summary
    logger.info("=" * 60)
    logger.info("EXPORT SUMMARY")
    logger.info("=" * 60)
    accounts = set(r["account"] for r in all_results)
    for account in accounts:
        count = sum(1 for r in all_results if r["account"] == account)
        checked = sum(1 for r in all_results if r["account"] == account and r["status"] == "checked")
        unchecked = sum(1 for r in all_results if r["account"] == account and r["status"] == "unchecked")
        indeterminate = sum(1 for r in all_results if r["account"] == account and r["status"] == "indeterminate")
        no_cb = sum(1 for r in all_results if r["account"] == account and r["status"] == "no_checkbox")
        logger.info(f"  {account}: {count} nodes ({checked} checked, {unchecked} unchecked, "
                     f"{indeterminate} indeterminate, {no_cb} no checkbox)")

    logger.info(f"Output file: {export_config['output']}")
    logger.info(f"Log file: {log_filename}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
