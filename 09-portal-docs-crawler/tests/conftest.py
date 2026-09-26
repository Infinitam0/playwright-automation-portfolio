"""Pin portal settings so tests do not depend on a developer's local .env."""

import os

# Set before the app modules are imported; load_dotenv() never overrides existing vars.
os.environ["PORTAL_BASE_URL"] = "https://support.example.com"
os.environ["PORTAL_TITLE_SUFFIX"] = "Example Portal"
os.environ["PORTAL_LOGIN_PATH"] = "/login"
os.environ["PORTAL_HOME_PATH"] = "/home"
os.environ["PORTAL_ARTICLE_PATH"] = "/articles/"
os.environ["PORTAL_CATEGORY_PATH"] = "/categories/"
