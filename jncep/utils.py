from collections import deque
from datetime import timezone
import html
import inspect
import logging
from pathlib import Path
import re
import sys
import unicodedata

from addict import Dict as Addict
import dateutil.parser
import rich.console
from rich.table import Column, Table
import rich.theme

logger = logging.getLogger(__name__)


def setup_logging(is_debug):
    format = "%(asctime)s %(name)-12s %(levelname)-8s %(message)s"
    if not logging.getLogger().handlers:
        # coloredlogs is not used
        logging.basicConfig(stream=sys.stdout, level=logging.INFO, format=format)
    else:
        # Colored_logs has installed its own handler but not configured the way
        # I want
        logging.getLogger().handlers[0].formatter.datefmt = "%Y-%m-%d,%H:%M:%S,%f"
        # coloredlogs changes the level of the handler
        logging.getLogger().handlers[0].setLevel(logging.NOTSET)

    logger_pkg = logging.getLogger(__package__)
    if is_debug:
        logger_pkg.setLevel(logging.DEBUG)
        # keep debug console (ie logging)
        # or issues mixing the print and the logs with Rich
    else:
        logger_pkg.setLevel(logging.INFO)
        getConsole().console = RichConsole()


def is_debug():
    logger_pkg = logging.getLogger(__package__)
    return logger_pkg.level <= logging.DEBUG


def tryint(val):
    try:
        return int(val)
    except Exception:
        return None


def to_yn(b):
    return "yes" if b else "no"


def to_safe_filename(name, char_replace="_", preserve_chars=""):
    name = _uni_nfd(name)
    safe = re.sub(
        r"[^0-9a-zA-Z" + re.escape(preserve_chars) + r"]+", char_replace, name
    )
    safe = safe.strip(char_replace)
    return safe


def to_safe_foldername(name, char_replace="_"):
    name = _uni_nfd(name)
    safe = re.sub(r'[<>:"/\\|?*]+', char_replace, name)
    safe = safe.strip(char_replace)
    return safe


def _uni_nfd(name):
    return "".join(
        c for c in unicodedata.normalize("NFD", name) if unicodedata.category(c) != "Mn"
    )


def module_info():
    # for main module : its __name__ is __main__
    # so find out its real name
    frm = inspect.stack()[1]
    mod = inspect.getmodule(frm[0])
    return mod.__spec__.name


def isoformat_with_z(d):
    # like the date format used by the JNC API
    if d.tzinfo != timezone.utc:
        # just in case but shouldn't happen : only date in UTC is used in the code
        raise ValueError("Date not in UTC!")
    return d.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compare_date_isoformat(d1, d2):
    # convert in case ms are used
    date1 = dateutil.parser.parse(d1)
    date2 = dateutil.parser.parse(d2)

    if date1 == date2:
        return 0
    elif date1 < date2:
        return -1
    return 1


def ensure_directory_exists(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def deep_freeze(data):
    if type(data) is Addict:
        data.freeze()
        for value in data.values():
            if type(value) is list:
                for v in value:
                    deep_freeze(v)


rich_theme = rich.theme.Theme(
    {
        "success": "green",
        "warning": "yellow",
        "error": "red",
        "highlight": "magenta",
        "important": "bold",
    }
)


class RichConsole:
    def __init__(self):
        self.console = rich.console.Console(
            highlight=False, theme=rich_theme, soft_wrap=True
        )
        self._status = None

    def info(self, *args, **kwargs):
        self.console.print(*args, **kwargs)

    def warning(self, *args, **kwargs):
        self.console.print(*args, **{"style": "warning", **kwargs})

    def error(self, *args, **kwargs):
        self.console.print(*args, **{"style": "error", **kwargs})

    def status(self, message, **kwargs_spinner_style):
        if not self._status:
            default = {}
            if self.is_advanced():
                default = {"spinner": "dots"}
            else:
                default = {"spinner": "line", "refresh_per_second": 6}
            st_args = {**default, **kwargs_spinner_style}

            self._status = self.console.status(message, **st_args)
            self._status.start()
        else:
            self._status.update(message, **kwargs_spinner_style)

    def stop_status(self):
        if self._status:
            self._status.stop()

    def log(self, *args, **kwargs):
        self.console.log(*args, **kwargs)

    def is_advanced(self):
        return not self.console.legacy_windows

    def info_table(self, rows, maxcolwidths):
        if not rows:
            return
        num_cols = len(rows[0])
        columns = [Column() for _ in range(num_cols)]

        if maxcolwidths:
            for i, c in enumerate(columns):
                c.width = maxcolwidths[i]

        table = Table.grid(*columns)
        for row in rows:
            table.add_row(*[str(c) for c in row])

        self.info(table)


class DebugConsole:
    def __init__(self):
        pass

    def info(self, message, *args, **kwargs):
        logger.info(message)

    def warning(self, message, *args, **kwargs):
        logger.warning(message)

    def error(self, message, *args, **kwargs):
        logger.error(message)

    def status(self, message, **kwargs_spinner_style):
        logger.warning(message)

    def stop_status(self):
        pass

    def log(self, message, *args, **kwargs):
        logger.info(message)

    def is_advanced(self):
        return False

    def info_table(self, rows, maxcolwidths):
        for row in rows:
            self.info("|".join([str(c) for c in row]))


class RootConsole:
    def __init__(self):
        # default
        self.console = DebugConsole()
        self.stack_status = deque()

    def info(self, message, *args, **kwargs):
        self.console.info(message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        self.console.warning(message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        self.console.error(message, *args, **kwargs)

    def status(self, message, clear=True, **kwargs_spinner_style):
        if clear:
            self.stack_status.clear()
        self.stack_status.append((message, kwargs_spinner_style))
        self.console.status(message, **kwargs_spinner_style)

    def pop_status(self):
        self.stack_status.pop()
        message, kwargs_spinner_style = self.stack_status[-1]
        self.console.status(message, **kwargs_spinner_style)

    def stop_status(self):
        self.console.stop_status()

    def log(self, message, *args, **kwargs):
        self.console.log(message, *args, **kwargs)

    def is_advanced(self):
        return self.console.is_advanced()

    def info_table(self, rows, maxcolwidths=None):
        self.console.info_table(rows, maxcolwidths)


ROOT_CONSOLE = RootConsole()


def getConsole():
    return ROOT_CONSOLE


def split_by_chapter(content, cover_filename=None):
    # Returns (contents, titles) with one entry per <h1> chapter, or None if the
    # parts have no <h1> at all (caller should then keep one entry per part)
    combined_content = ""
    chapter_titles = []
    first_before_content = ""
    first_after_content = ""
    h1_title_pattern = r"<h1[^>]*>(.*?)</h1>"
    returned_content = []
    title_counts = {}

    for i, c in enumerate(content):  # iterate over each part
        # Separate out the main content from the rest
        before_content, inside_content, after_content = _split_main_div(c)
        combined_content += inside_content.strip()
        if i == 0:
            first_before_content = before_content.strip()
            first_after_content = after_content.strip()

    # split the combined text into chapters
    parts = re.split(r"(?=<h1[^>]*>)", combined_content, flags=re.DOTALL)
    if not any(re.match(r"<h1[^>]*>", part) for part in parts):
        logger.debug("No <h1> found: keeping the parts as chapters")
        return None

    for part in parts:
        # Find the title in the chapter
        titles = re.findall(h1_title_pattern, part, re.DOTALL)

        if len(titles) > 0:
            title = _heading_text(titles[0])
        else:
            # Only the text before the first <h1> has no title. At the start of a
            # volume, it holds the cover (already in the EPUB as its own page) and
            # sometimes color illustrations
            if cover_filename:
                cover_img_pattern = (
                    rf"<img\b[^>]*\bsrc=[\"']{re.escape(cover_filename)}[\"'][^>]*>"
                )
                part = re.sub(cover_img_pattern, "", part)
            if re.sub(r"<[^>]+>", "", part).strip():
                # download starts in the middle of a chapter
                title = "Continued from previous part"
            elif "<img" in part:
                title = "Illustrations"
            else:
                continue

        # some sections repeat (ex: Q&A at the end of each part of a fanbook)
        title_counts[title] = title_counts.get(title, 0) + 1
        if title_counts[title] > 1:
            title = f"{title} ({title_counts[title]})"

        chapter_titles.append(title)
        reconstructed_content = (
            re.sub(
                r"<title>.*?</title>",
                lambda _: f"<title>{html.escape(title)}</title>",
                first_before_content,
                flags=re.DOTALL,
            )
            + part
            + first_after_content
        )
        returned_content.append(reconstructed_content)
    return returned_content, chapter_titles


def _heading_text(heading_html):
    # h1 can contain markup (em, br...) and entities (&amp;): the TOC needs plain
    # text. A <br/> separates a title from its subtitle
    text = re.sub(r"<br\s*/?>", ": ", heading_html)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\s+", " ", text).strip(" :")


def _split_main_div(c):
    # Returns (before, inside, after) for the <div class="main"> of a part.
    # The closing tag is found by counting nested divs: a non-greedy regex would
    # stop at the first </div> and drop the rest of the part.
    start = re.search(r"<div[^>]*\bclass=[\"'][^\"']*\bmain\b[^\"']*[\"'][^>]*>", c)
    if start is None:
        # no main div: keep the whole body rather than dropping the part
        logger.warning("Part without <div class=\"main\">: using whole body")
        body = re.search(r"^(.*?<body[^>]*>)(.*)(</body>.*)$", c, re.DOTALL)
        if body is None:
            return "", c, ""
        return body.group(1), body.group(2), body.group(3)

    depth = 1
    for tag in re.finditer(r"<(/?)div\b[^>]*>", c[start.end() :], re.IGNORECASE):
        if tag.group(0).endswith("/>"):
            continue
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            inside_end = start.end() + tag.start()
            after_start = start.end() + tag.end()
            return c[: start.start()], c[start.end() : inside_end], c[after_start:]

    # unclosed main div: everything after it is the content
    logger.warning("Unclosed <div class=\"main\">: using rest of part")
    return c[: start.start()], c[start.end() :], ""

