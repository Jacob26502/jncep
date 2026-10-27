from __future__ import annotations

from .. import core, jncweb, track, utils
from ..utils import tryint

console = utils.getConsole()


def resolve_jnc_url_or_index(jnc_url_or_index, tracked_series=None):
    index = tryint(jnc_url_or_index)
    if index is None:
        return jnc_url_or_index

    if tracked_series is None:
        track_manager = track.TrackConfigManager()
        tracked_series = track_manager.read_tracked_series()

    index0 = index - 1
    if index0 < 0 or index0 >= len(tracked_series):
        console.warning(f"Index '{index}' is not valid! (Use 'track list')")
        return None

    series_url_list = list(tracked_series.keys())
    jnc_url = series_url_list[index0]
    series_name = tracked_series[jnc_url].name
    console.info(f"Resolve to series '[highlight]{series_name}'[/]")
    return jnc_url


async def fetch_series_from_url(session, jnc_url):
    jnc_resource = jncweb.resource_from_url(jnc_url)
    series_id = await core.resolve_series(session, jnc_resource)
    series = await core.fetch_meta(session, series_id)
    core.check_series_is_novel(series)
    return series, jnc_resource
