import logging
import os
import re
from typing import Dict, List, Optional, Tuple

import pystac
from pystac.extensions.sat import OrbitState, SatExtension
from pystac.extensions.eo import EOExtension
from pystac.extensions.projection import ProjectionExtension

from stactools.core.io import ReadHrefModifier
from stactools.core.projection import transform_from_bbox
from stactools.sentinel2.safe_manifest import SafeManifest
from stactools.sentinel2.product_metadata import ProductMetadata
from stactools.sentinel2.granule_metadata import GranuleMetadata
from stactools.sentinel2.utils import extract_gsd
from stactools.sentinel2.constants import (DATASTRIP_METADATA_ASSET_KEY,
                                           SENTINEL_PROVIDER, SENTINEL_LICENSE,
                                           SENTINEL_BANDS,
                                           SENTINEL_INSTRUMENTS,
                                           SENTINEL_CONSTELLATION,
                                           INSPIRE_METADATA_ASSET_KEY)

logger = logging.getLogger(__name__)


def create_item(
        granule_href: str,
        additional_providers: Optional[List[pystac.Provider]] = None,
        read_href_modifier: Optional[ReadHrefModifier] = None) -> pystac.Item:
    """Create a STC Item from a Sentinel 2 granule.

    Arguments:
        granule_href: The HREF to the granule. This is expected to be a path
            to a SAFE archive, e.g. : https://sentinel2l2a01.blob.core.windows.net/sentinel2-l2/01/C/CV/2016/03/27/S2A_MSIL2A_20160327T204522_N0212_R128_T01CCV_20210214T042702.SAFE
        additional_providers: Optional list of additional providers to set into the Item
        read_href_modifier: A function that takes an HREF and returns a modified HREF.
            This can be used to modify a HREF to make it readable, e.g. appending
            an Azure SAS token or creating a signed URL.

    Returns:
        pystac.Item: An item representing the Sentinel 2 scene
    """ # noqa

    safe_manifest = SafeManifest(granule_href, read_href_modifier)

    product_metadata = ProductMetadata(safe_manifest.product_metadata_href,
                                       read_href_modifier)
    granule_metadata = GranuleMetadata(safe_manifest.granule_metadata_href,
                                       read_href_modifier)

    item = pystac.Item(id=product_metadata.product_id,
                       geometry=product_metadata.geometry,
                       bbox=product_metadata.bbox,
                       datetime=product_metadata.datetime,
                       properties={})

    # --Common metadata--

    item.common_metadata.providers = [SENTINEL_PROVIDER]

    if additional_providers is not None:
        item.common_metadata.providers.extend(additional_providers)

    item.common_metadata.platform = product_metadata.platform
    item.common_metadata.constellation = SENTINEL_CONSTELLATION
    item.common_metadata.instruments = SENTINEL_INSTRUMENTS

    # --Extensions--

    # eo

    eo = EOExtension.ext(item)
    eo.cloud_cover = granule_metadata.cloudiness_percentage

    # sat
    sat = SatExtension.ext(item)
    sat.orbit_state = OrbitState(product_metadata.orbit_state.lower())
    sat.relative_orbit = product_metadata.relative_orbit

    # proj
    projection = ProjectionExtension.ext(item)
    projection.epsg = granule_metadata.epsg
    if projection.epsg is None:
        raise ValueError(
            f'Could not determine EPSG code for {granule_href}; which is required.'
        )

    # s2 properties
    item.properties.update({
        **product_metadata.metadata_dict,
        **granule_metadata.metadata_dict
    })

    # --Assets--

    # Metadata

    item.add_asset(*safe_manifest.create_asset())
    item.add_asset(*product_metadata.create_asset())
    item.add_asset(*granule_metadata.create_asset())
    item.add_asset(
        INSPIRE_METADATA_ASSET_KEY,
        pystac.Asset(href=safe_manifest.inspire_metadata_href,
                     media_type=pystac.MediaType.XML,
                     roles=['metadata']))
    item.add_asset(
        DATASTRIP_METADATA_ASSET_KEY,
        pystac.Asset(href=safe_manifest.datastrip_metadata_href,
                     media_type=pystac.MediaType.XML,
                     roles=['metadata']))

    # Image assets
    proj_bbox = granule_metadata.proj_bbox

    image_assets = dict([
        image_asset_from_href(os.path.join(granule_href, image_path), item,
                              granule_metadata.resolution_to_shape, proj_bbox,
                              product_metadata.image_media_type)
        for image_path in product_metadata.image_paths
    ])

    for key, asset in image_assets.items():
        assert key not in item.assets
        item.add_asset(key, asset)

    # Thumbnail

    if safe_manifest.thumbnail_href is not None:
        item.add_asset(
            "preview",
            pystac.Asset(href=safe_manifest.thumbnail_href,
                         media_type=pystac.MediaType.COG,
                         roles=['thumbnail']))

    # --Links--

    item.links.append(SENTINEL_LICENSE)

    return item


def image_asset_from_href(
        asset_href: str,
        item: pystac.Item,
        resolution_to_shape: Dict[int, Tuple[int, int]],
        proj_bbox: List[float],
        media_type: Optional[str] = None) -> Tuple[str, pystac.Asset]:
    logger.debug(f'Creating asset for image {asset_href}')

    _, ext = os.path.splitext(asset_href)
    if media_type is not None:
        asset_media_type = media_type
    else:
        if ext.lower() == '.jp2':
            asset_media_type = pystac.MediaType.JPEG2000
        elif ext.lower() in ['.tiff', '.tif']:
            asset_media_type = pystac.MediaType.GEOTIFF
        else:
            raise Exception(
                f'Must supply a media type for asset : {asset_href}')

    # Handle preview image

    if '_PVI' in asset_href:
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title='True color preview',
                             roles=['data'])
        asset_eo = EOExtension.ext(asset)
        asset_eo.bands = [
            SENTINEL_BANDS['B04'], SENTINEL_BANDS['B03'], SENTINEL_BANDS['B02']
        ]
        return ('preview', asset)

    # Extract gsd and proj info
    gsd = extract_gsd(asset_href)
    shape = list(resolution_to_shape[int(gsd)])
    transform = transform_from_bbox(proj_bbox, shape)

    def set_asset_properties(asset):
        item.common_metadata.set_gsd(gsd, asset)
        asset_projection = ProjectionExtension.ext(asset)
        asset_projection.shape = shape
        asset_projection.bbox = proj_bbox
        asset_projection.transform = transform

    # Handle band image

    band_id_search = re.search(r'_(B\w{2})_', asset_href)
    if band_id_search is not None:
        band_id = band_id_search.group(1)
        band = SENTINEL_BANDS[band_id]
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title=band.description,
                             roles=['data'])
        asset_eo = EOExtension.ext(asset)
        asset_eo.bands = [SENTINEL_BANDS[band_id]]
        set_asset_properties(asset)
        return (band_id, asset)

    # Handle auxiliary images

    if '_TCI_' in asset_href:
        # True color
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title='True color image',
                             roles=['data'])
        asset_eo = EOExtension.ext(asset)
        asset_eo.bands = [
            SENTINEL_BANDS['B04'], SENTINEL_BANDS['B03'], SENTINEL_BANDS['B02']
        ]
        set_asset_properties(asset)
        return (f'visual-{asset_href[-7:-4]}', asset)

    if '_AOT_' in asset_href:
        # Aerosol
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title='Aerosol optical thickness (AOT)',
                             roles=['data'])
        set_asset_properties(asset)
        return (f'AOT-{asset_href[-7:-4]}', asset)

    if '_WVP_' in asset_href:
        # Water vapor
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title='Water vapour (WVP)',
                             roles=['data'])
        set_asset_properties(asset)
        return (f'WVP-{asset_href[-7:-4]}', asset)

    if '_SCL_' in asset_href:
        # Classification map
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title='Scene classfication map (SCL)',
                             roles=['data'])
        set_asset_properties(asset)
        return (f'SCL-{asset_href[-7:-4]}', asset)

    raise ValueError(f'Unexpected asset: {asset_href}')
