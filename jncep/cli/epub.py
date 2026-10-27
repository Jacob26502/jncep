import logging

import click

from .. import core, jncalts, namegen, spec, utils
from ..config import ENVVAR_PREFIX
from ..trio_utils import coro
from . import options
from .base import CatchAllExceptionsCommand
from .series import fetch_series_from_url, resolve_jnc_url_or_index

logger = logging.getLogger(__name__)
console = utils.getConsole()


@click.command(
    name="epub",
    help="Generate EPUB files for J-Novel Club pre-pub novels",
    cls=CatchAllExceptionsCommand,
)
@click.argument("jnc_url_or_index", metavar="JNOVEL_CLUB_URL", required=True)
@options.credentials_options
@options.output_option
@click.option(
    "-s",
    "--parts",
    "part_spec",
    help=(
        "Specification of a range of parts to download in the form of "
        "<vol>[.part]:<vol>[.part] [default: All the content linked by "
        "the JNOVEL_CLUB_URL argument, either a single part, a whole volume "
        "or the whole series]"
    ),
)
@options.byvolume_option
@options.subfolder_option
@options.images_option
@options.raw_content_option
@options.no_replace_chars_option
@options.css_option
@options.namegen_option
@options.replace_partial_option
@options.folder_map_option
@click.option(
    "-w",
    "--whole",
    "is_whole_volume",
    is_flag=True,
    envvar=f"{ENVVAR_PREFIX}WHOLE",
    help=(
        "Flag to indicate whether the whole volume (all its available parts) "
        "should be generated when the JNOVEL_CLUB_URL argument is a part"
    ),
)
@coro
async def generate_epub(
    jnc_url_or_index,
    credentials: jncalts.AltCredentials,
    part_spec,
    output_dirpath,
    is_by_volume,
    is_subfolder,
    is_extract_images,
    is_extract_content,
    is_not_replace_chars,
    style_css_path,
    namegen_rules,
    is_replace_partial,
    folder_map,
    is_whole_volume,
):
    epub_generation_options = create_epub_generation_options(
        output_dirpath,
        is_subfolder,
        is_by_volume,
        is_extract_images,
        is_extract_content,
        is_not_replace_chars,
        style_css_path,
        namegen_rules,
        is_replace_partial,
        folder_map,
    )

    jnc_url = resolve_jnc_url_or_index(jnc_url_or_index)
    if not jnc_url:
        return

    origin = jncalts.find_origin(jnc_url)
    config = jncalts.get_alt_config_for_origin(origin)

    async with core.JNCEPSession(config, credentials) as session:
        series, jnc_resource = await fetch_series_from_url(session, jnc_url)

        await generate_epubs_for_resource(
            session,
            series,
            jnc_resource,
            part_spec,
            epub_generation_options,
            is_whole_volume,
        )


def create_epub_generation_options(
    output_dirpath,
    is_subfolder,
    is_by_volume,
    is_extract_images,
    is_extract_content,
    is_not_replace_chars,
    style_css_path,
    namegen_rules,
    is_replace_partial=False,
    folder_map=None,
):
    name_generator = namegen.NameGenerator(namegen_rules)
    return core.EpubGenerationOptions(
        output_dirpath,
        is_subfolder,
        is_by_volume,
        is_extract_images,
        is_extract_content,
        is_not_replace_chars,
        style_css_path,
        name_generator,
        is_replace_partial,
        folder_map,
    )


async def generate_epubs_for_resource(
    session,
    series,
    jnc_resource,
    part_spec,
    epub_generation_options,
    is_whole_volume=False,
):
    if part_spec:
        console.info(f"Use part specification '[highlight]{part_spec}[/]'")
        part_spec_analyzed = spec.analyze_part_specs(part_spec)
        part_spec_analyzed.normalize_and_verify(series)
    else:
        part_spec_analyzed = await core.to_part_spec(series, jnc_resource)
        if is_whole_volume and part_spec_analyzed.type_ == spec.PART:
            console.info("Generate the whole volume of the part")
            part_spec_analyzed = spec.IdentifierSpec(
                spec.VOLUME, part_spec_analyzed.volume_id
            )

    await generate_epubs(session, series, part_spec_analyzed, epub_generation_options)


async def generate_epubs(session, series, part_spec_analyzed, epub_generation_options):
    console.status("Get content...")

    has_unavailable_parts = False

    # TODO do not use filter before trying to download : try to download and see if
    # rejected : print warning or error depending on command and if there can be no
    # EPUB file generated (no download)
    # may need to still filter for parts in future
    def part_filter(part):
        if part_spec_analyzed.has_part(part):
            if core.is_part_available(session.now, session.member_status, part):
                return True
            else:
                nonlocal has_unavailable_parts
                has_unavailable_parts = True
        return False

    (
        volumes_to_download,
        parts_to_download,
    ) = core.relevant_volumes_and_parts_for_content(series, part_filter)

    if not parts_to_download:
        console.error(
            "None of the requested parts are available! No EPUB will be generated.",
        )
        return

    if has_unavailable_parts:
        console.warning(
            "Some of the requested parts are not available for reading!",
        )

    volumes_for_cover = core.relevant_volumes_for_cover(
        volumes_to_download, epub_generation_options.is_by_volume
    )

    await core.fill_covers_and_content(session, volumes_for_cover, parts_to_download)

    has_missing, has_available = core.has_missing_part_content(parts_to_download)
    if has_missing:
        if has_available:
            console.warning(
                "Some parts were not downloaded correctly! Do you have a subscription?",
            )
            # continue: can generated an Epub with the downloaded parts
        else:
            console.error(
                "None of the parts were downloaded correctly! "
                "Do you have a subscription?",
            )
            return

    console.status("Create EPUB...")

    await core.create_epub(
        series,
        volumes_to_download,
        parts_to_download,
        epub_generation_options,
    )
