from collections import OrderedDict
from functools import partial
import json
import logging
from pathlib import Path

from addict import Dict as Addict
from atomicwrites import atomic_write
import dateutil.parser
import trio

from . import core, jncweb, spec
from .trio_utils import background, gather
from .utils import green

logger = logging.getLogger(__package__)


# TODO change => in App folder for windows + config file there too
CONFIG_DIRPATH = Path.home() / ".jncep"


def read_tracked_series():
    try:
        with _tracked_series_filepath().open() as json_file:
            # Explicit ordereddict (although should be fine without
            # since Python >= 3.6 dicts are ordered ; spec since 3.7)
            data = json.load(json_file, object_pairs_hook=OrderedDict)
            return _convert_to_latest_format(Addict(data))
    except FileNotFoundError:
        # first run ?
        return Addict({})


def _convert_to_latest_format(data):
    converted = {}
    # while at it convert from old format
    # legacy format for tracked parts : just the part instead of object
    # with keys part, name
    # key is slug
    # TODO rename "name" field into "title"
    for series_url_or_slug, value in data.items():
        if not isinstance(value, dict):
            series_slug = series_url_or_slug
            series_url = jncweb.url_from_series_slug(series_slug)
            # low effort way to get some title
            name = series_slug.replace("-", " ").title()
            value = Addict({"name": name, "part": value})
            converted[series_url] = value
        else:
            converted[series_url_or_slug] = value

    converted_b = {}
    for legacy_series_url, value in converted.items():
        new_series_url = jncweb.to_new_website_series_url(legacy_series_url)
        converted_b[new_series_url] = value

    return converted_b


def write_tracked_series(tracked):
    _ensure_config_dirpath_exists()
    with atomic_write(str(_tracked_series_filepath().resolve()), overwrite=True) as f:
        f.write(json.dumps(tracked, sort_keys=True, indent=2))


def _tracked_series_filepath():
    return CONFIG_DIRPATH / "tracked.json"


def _ensure_config_dirpath_exists():
    CONFIG_DIRPATH.mkdir(parents=False, exist_ok=True)


async def fill_meta_for_track(session, series):
    volumes = await core.fetch_volumes_meta(session, series.series_id)
    series.volumes = volumes
    for volume in volumes:
        volume.series = series

    if volumes:
        # just the last
        last_volume = volumes[-1]
        parts = await core.fetch_parts_meta(session, last_volume.volume_id)
        last_volume.parts = parts
        for part in parts:
            part.volume = last_volume

    # similar to what we got from the original JNC API
    return series


async def track_series(session, tracked_series, series):
    await fill_meta_for_track(session, series)

    last_part = None
    if series.volumes:
        last_volume = series.volumes[-1]
        if last_volume.parts:
            last_part = last_volume.parts[-1]

    # record current last part + name
    if not last_part:
        # no parts yet
        pn = 0
        # 0000-... not a valid date so 1111-...
        pdate = "1111-11-11T11:11:11.111Z"

        # TODO event
        logger.info(
            green(
                f"The series '{series.raw_data.title}' is now tracked, starting "
                f"from the beginning"
            )
        )
    else:
        pn = spec.to_relative_spec_from_part(last_part)
        pdate = last_part.raw_data.launch

        relative_part = spec.to_relative_spec_from_part(last_part)
        part_date = dateutil.parser.parse(last_part.raw_data.launch)
        part_date_formatted = part_date.strftime("%b %d, %Y")
        logger.info(
            green(
                f"The series '{series.raw_data.title}' is now tracked, starting "
                f"after part {relative_part} [{part_date_formatted}]"
            )
        )

    series_url = jncweb.url_from_series_slug(series.raw_data.slug)
    tracked_series[series_url] = {
        "part_date": pdate,
        "part": pn,  # now just for show
        "name": series.raw_data.title,
    }


async def sync_series_forward(session, follows, tracked_series, is_delete):
    # sync local tracked series based on remote follows
    new_synced = []
    del_synced = []
    async with trio.open_nursery() as n:
        f_tasks = []
        for jnc_resource in follows:
            if jnc_resource.url in tracked_series:
                continue

            async def do_track(jnc_resource):
                series = await core.resolve_series(session, jnc_resource)
                await track_series(session, tracked_series, series)

                series_url = jncweb.url_from_series_slug(series.raw_data.slug)
                new_synced.append(series_url)

            f_task = background(n, partial(do_track, jnc_resource))
            f_tasks.append(f_task)

        # result doesn't matter ; just drain exceptions
        await gather(n, f_tasks).get()

    if is_delete:
        followed_index = {f.url: f for f in follows}
        # list() to avoid dictionary changed size during iteration
        for series_url, series_data in list(tracked_series.items()):
            if series_url not in followed_index:
                del tracked_series[series_url]

                logger.warning(f"The series '{series_data.name}' is no longer tracked")

                del_synced.append(series_url)

    return new_synced, del_synced


async def sync_series_backward(session, follows, tracked_series, is_delete):
    # sync remote follows based on locally tracked series
    new_synced = []
    del_synced = []

    followed_index = {f.url: f for f in follows}
    async with trio.open_nursery() as n:
        f_tasks = []
        for series_url in tracked_series:
            # series_url is the latest URL format (same as the follows)
            if series_url in followed_index:
                continue

            jnc_resource = jncweb.resource_from_url(series_url)

            async def do_follow(jnc_resource):
                # TODO make sure it is next to follow ?
                logger.info(f"Fetching metadata for '{jnc_resource}'...")
                series = await core.resolve_series(session, jnc_resource)
                series_id = series.series_id
                title = series.raw_data.title

                logger.info(f"Follow '{title}'...")
                await session.api.follow_series(series_id)

                new_synced.append(series_url)

            f_task = background(n, partial(do_follow, jnc_resource))
            f_tasks.append(f_task)

        if is_delete:
            for jnc_resource in follows:
                if jnc_resource.url not in tracked_series:

                    async def do_undollow(jnc_resource):
                        # use the follow_raw_data: to avoid another call to the API
                        series_id = jnc_resource.follow_raw_data.id
                        title = jnc_resource.follow_raw_data.title
                        logger.warning(f"Unfollow '{title}'...")
                        await session.api.unfollow_series(series_id)

                        del_synced.append(jnc_resource.url)

                    f_task = background(n, partial(do_undollow, jnc_resource))
                    f_tasks.append(f_task)

        await gather(n, f_tasks).get()

    return new_synced, del_synced
