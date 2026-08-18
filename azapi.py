import base64
import functools
import json
import logging
import os
import re
import secrets
import time
import typing
import uuid
import concurrent.futures
from datetime import datetime, timedelta
from enum import Enum, auto
from urllib.parse import parse_qs, urlencode
from xml.etree import ElementTree

import httpx
import rsa
import rsa.pkcs1
from audible.login import (
    build_device_serial,
    create_code_verifier,
    create_s256_code_challenge,
)
from Crypto.PublicKey import RSA

from .models import AmazonMusicDevice, AmazonMusicMobileAPICredentials, AmazonMusicTier, AmazonRegion, AmazonContinent

LOGGER = logging.getLogger(__name__)

# Some credit names from the xray API are not formatted correctly
CREDIT_NAME_FIXES = {
    "Performed By": "Performer",
    "Written By": "Lyricist",
    "Produced By": "Producer",
    "Music Publisher": "Publisher",
}


class AmazonMobileApplication(Enum):
    MUSIC = auto()
    PRIME_VIDEO = auto()

    @property
    def device_type(self):
        return {
            self.MUSIC: "A1DL2DVDQVK3Q",
            self.PRIME_VIDEO: "A43PXU4ZN2AL1",
        }[self]

    @property
    def assoc_handle(self):
        return {
            self.MUSIC: "amzn_tiburon_na",
            self.PRIME_VIDEO: "amzn_piv_android_v2_us",
        }[self]

    @property
    def official_name(self):
        return {
            self.MUSIC: "Amazon Music",
            self.PRIME_VIDEO: "Amazon Prime Video",
        }[self]


class AmazonMusicMobileAPI:
    """Amazon Music API"""

    application_version = "22.15.12"
    harley_version = "3.12.3.86"

    HARLEY_USER_AGENT = f"Harley/{harley_version} {AmazonMobileApplication.MUSIC.device_type}/{application_version}"
    """ Used for accessing playing DRM protected content """
    APP_USER_AGENT = f"MusicAndroid/{application_version}"
    """ Used for API requests """

    USER_AGENT = "Mozilla/5.0 (Linux; Android 11; Pixel 5 Build/RD2A.211001.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/108.0.5359.128 Mobile Safari/537.36"
    """ Used for Amazon login & other general requests """

    credentials: AmazonMusicMobileAPICredentials

    def __init__(
        self,
        credentials: AmazonMusicMobileAPICredentials,
    ) -> None:
        self.credentials = credentials
        self.session = self._create_httpx_session()
        self.session.cookies.update(credentials.website_cookies)

        if not self.credentials.account_region:
            self.credentials.account_region = AmazonRegion.get_region_by_country(
                dict(self.get_account_status()).get("customerAccount", {}).get("accountInfo", {}).get("musicTerritory", "")
            )

        # Always update the tier on instance creation
        self.credentials.tier = self.get_account_subscription_tier()

    @classmethod
    def login_via_mobile(
        cls,
        email: str,
        password: str,
        country_code: str = "US",
        serial: typing.Optional[str] = None,
        load_credentials: typing.Optional[bool] = True,
        application: typing.Optional[AmazonMobileApplication] = None,
        oauth_flow_callback: typing.Optional[typing.Callable[[str, str], str]] = None,
    ):
        if len(country_code) != 2:
            raise ValueError(
                f"Country code must be a ISO 3166-1 alpha-2 value!, got: {country_code}"
            )
        selected_region = AmazonRegion.get_region_by_country(country_code)
        application = application or AmazonMobileApplication.MUSIC

        session = cls._create_httpx_session()

        if country_code == "JP" and application is not AmazonMobileApplication.PRIME_VIDEO:
            # Login to Prime Video first, because amazon.
            session = cls.login_via_mobile(
                email=email,
                password=password,
                load_credentials=False,
                application=AmazonMobileApplication.PRIME_VIDEO,
                country_code=country_code,
            )

        base_url = f"https://amazon.{selected_region.domain_tld}"
        init_cookies = cls._build_init_cookies()

        session.base_url = base_url
        session.cookies.update(init_cookies)

        code_verifier = create_code_verifier()

        oauth_url, serial = cls._build_oauth_url(
            domain="com",
            code_verifier=code_verifier,
            application=application,
            serial=serial,
            selected_region=selected_region
        )

        authorization_code = cls._external_login(
            oauth_url, application, oauth_flow_callback=oauth_flow_callback
        )

        if not load_credentials:
            return session

        inst = cls.register(
            application=application,
            selected_region=selected_region,
            authorization_code=authorization_code,
            code_verifier=code_verifier,
            serial=serial,
        )
        print(
            f"Login confirmed for {inst.credentials.customer_info.get('name', 'Unknown user')} in {selected_region.pretty_name} on {application.official_name}"
        )

        # Authorize device for usage on Amazon Music
        auth_device_resp = dict(inst.authorize_device(device_serial=serial).json())
        inst.credentials.customer_id = auth_device_resp["device"]["customerId"]

        # TODO add a check if too many devices are registered, and if so, notify the user and add a way to remove devices via a prompt
        inst._list_devices()

        if not inst.credentials:
            raise Exception("Login failed. Please check the log.")
        return inst

    @staticmethod
    def _wait_for_response(session: httpx.Client, request: httpx.Request):
        # Sometimes we get a DNS resolve error (too many requests for manifest?), this attempts to retry 5 times
        resp = None
        last_http_exc = None
        for _ in range(6):
            try:
                LOGGER.debug("Handling request: %s", request)
                resp = session.send(request)
                resp.raise_for_status()
                LOGGER.debug(
                    "OK with request with status code %s for request %s", resp.status_code, request.url
                )
            except httpx.HTTPError as ce:
                if resp and resp.status_code == 400:
                    # this is usually an error with the user, than the server itself.
                    return resp
                LOGGER.error(ce)
                if resp:
                    LOGGER.error(str(resp.content))
                LOGGER.debug(ce, exc_info=True)
                last_http_exc = ce
                time.sleep(2)
            else:
                return resp
        if resp:
            LOGGER.error("%s, %s", resp.text, resp.content)
        raise last_http_exc or RuntimeError()

    def post(
        self,
        url: str,
        data: dict | None,
        headers: typing.Optional[dict] = None,
        add_default_stratus_headers: typing.Optional[bool] = True,
        sign: typing.Optional[bool] = True,
    ) -> httpx.Response:
        # these headers assume that the url is https://music.amazon.com/NA/api/stratus/
        if add_default_stratus_headers:
            headers = {
                "User-Agent": self.APP_USER_AGENT,
                "android-app-version": self.application_version,
                "content-encoding": "amz-1.0",
                "accept": "application/json",
                "accept-encoding": "gzip",
                "accept-charset": "utf-8",
                "content-type": "application/json; charset=UTF-8",
            } | (headers or {})
        request = httpx.Request("POST", url, headers=headers, json=data)
        if sign:
            self._apply_signing_auth_flow(request)
        httpx.Cookies(self.credentials.website_cookies).set_cookie_header(request)
        LOGGER.debug("Cookies auth flow applied to request")
        return self._wait_for_response(self.session, request)

    def _music_api_call(
        self,
        service: str,
        target: str,
        data: dict,
        region_to_use: typing.Optional[AmazonRegion] = None,
        user_agent: typing.Optional[str] = None,
        extra_headers: typing.Optional[dict] = None,
    ) -> httpx.Response:
        """POST to a https://music.amazon.<tld>/<region>/api/<service>/ endpoint."""
        region = region_to_use or self.credentials.account_region
        headers = {
            "x-amz-target": target,
            "x-amz-requestid": str(uuid.uuid4()),
        }
        if user_agent:
            headers["User-Agent"] = user_agent
        if extra_headers:
            headers |= extra_headers
        return self.post(
            url=f"https://music.amazon.{region.domain_tld}/{region.region.name}/api/{service}/",
            headers=headers,
            data=data,
        )

    @functools.lru_cache()
    def get_metadata(
        self,
        asins: str | typing.Sequence[str],
        use_alternative_naming: typing.Optional[bool] = None,
        region_to_use: typing.Optional[AmazonRegion] = None
    ) -> dict[str, list[dict[str, typing.Any]]]:
        """
        Get metadata for a track, album, playlist or artist.

        Track ASIN -> `response.json()['tracksList'][0]`

        Album ASIN -> `response.json()['albumsList'][0]`

        Artist ASIN -> `response.json()['artistList'][0]`
        """
        if not asins:
            raise ValueError(asins)
        if not region_to_use:
            region_to_use = self.credentials.account_region

        asins = [asins] if isinstance(asins, str) else list(asins)
        response = self._music_api_call(
            "muse",
            "com.amazon.musicensembleservice.MusicEnsembleService.lookup",
            {
                "allowedParentalControls": {"hasExplicitLanguage": True},
                "asins": asins,
                "currencyOfPreference": None,
                "customerIP": None,
                "customerId": None,
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": AmazonMobileApplication.MUSIC.device_type,
                # expandTracklist is intentionally duplicated (matches the app's request)
                "features": (
                    "ownership expandTracklist hasLyrics includeVideo requestAudioVideo "
                    "popularity expandTracklist fullAlbumDetails includePurchaseDetails "
                    "trackLibraryAvailability collectionLibraryAvailability "
                    "migratedLikeAvailability playlistLibraryAvailability"
                ).split(),
                "filters": None,
                "lang": region_to_use.locale,  # the lang locale of the phone/mobile app, en_US
                "marketplaceId": None,
                "debug": True,
                "metadataLang": "en"
                if use_alternative_naming
                else None,  # null for locale based on IP, setting to a random string value returns it romanized
                "musicRequestIdentityContextToken": None,
                "musicTerritory": region_to_use.country,
                "requestedContent": "FULL_CATALOG",  # ALL_STREAMABLE (for current account only), FULL_CATALOG is valid too
                "sessionId": None,
                "stub": None,
            },
            region_to_use=region_to_use,
        )
        if response.status_code != 200:
            raise Exception(
                f"Failed to get metadata: {response.status_code} {response.text}"
            )
        resp_json = response.json()

        LOGGER.debug(json.dumps(resp_json, indent=2))
        return resp_json

    def get_page(
        self,
        uri: str,
        count: typing.Optional[int] = None,
        next_token: typing.Optional[str] = None,
        offset: typing.Optional[int] = None,
        region_to_use: typing.Optional[AmazonRegion] = None
    ):
        """
        Get a page of a Amazon Music URI.

        Example usage:

        `self.mobile_session.get_page("album/B0CDJC65LH", count=0, locale="en_US")`
        """
        if not count:
            count = 5
        region = region_to_use or self.credentials.account_region

        resp = self._music_api_call(
            "musepage",
            "com.amazon.musicensembleservice.MusicEnsembleService.page",
            {
                "allowedParentalControls": {"hasExplicitLanguage": True},
                "allowedParentalControlsString": None,
                "artistVideoStoryEntityAsin": None,
                "browseId": None,
                "campaignsXml": None,
                "contentFeatures": (
                    "includeVideo includeVideoStory allowDeepLinkURLInWidget podcast "
                    "includePodcastCuratedContent includePodcastUserContent "
                    "includePodcastEpisodeDescriptiveShoveler podcastSonicRush includeLiveStream"
                ).split(),
                "count": count,
                "countOfEntitiesPerWidget": None,
                "customerIP": None,
                "customerId": None,
                "debug": None,  # set to True for.. a new errors attribute.
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": AmazonMobileApplication.MUSIC.device_type,
                "ipAddress": None,
                "languagesOfPerformance": None,
                "locale": region.locale,  # "ja_JP"
                "marketplaceId": None,
                "musicRequestIdentityContextToken": None,
                "musicTerritory": region.country,
                "nextToken": next_token,
                "offset": offset,
                "requestedContent": "KATANA",
                "sessionId": None,
                "stub": False,
                "testTraffic": None,
                "upsellContent": None,
                "uri": uri,  # e.g "album/B0CDJC65LH"
                "validationPayload": None,
            },
            region_to_use=region,
        )
        return dict(resp.json())

    def search(
        self,
        query: str,
        asins: typing.Optional[tuple[str]] = None,
        search_types: typing.Optional[tuple[str, ...]] = None,
        limit: typing.Optional[int] = 50,
        region_to_use: typing.Optional[AmazonRegion] = None,
    ):
        """
        Search for a item using a query.

        Args:
            asins: A tuple of str (Optional): Return the document which matched with the nth index of ASINs.
            search_types: Iterable (tuple) (Optional): Search for a specific catalog type.

        Valid types are:
        `catalog_album, catalog_artist, catalog_playlist, catalog_station,
        catalog_track, livesports_program, catalog_video, catalog_video_playlist,
        catalog_podcast_show, catalog_podcast_episode, live_event`
        """
        if search_types is None:
            search_types = ("catalog_album",)
        if not region_to_use:
            region_to_use = self.credentials.account_region

        requested_limit = int(limit) if isinstance(limit, int) and limit > 0 else 50
        page_size = max(1, min(requested_limit, 100))
        max_pages = 100
        page_tokens: dict[str, typing.Optional[str]] = {
            label_type: None for label_type in search_types
        }
        seen_asins: set[str] = set()
        collected_docs: list[dict[str, typing.Any]] = []

        def _next_token_for_category(category: dict) -> typing.Optional[str]:
            for token_key in ("nextPageToken", "pageToken", "nextToken"):
                token = category.get(token_key)
                if token:
                    return str(token)
            # Some responses nest tokens under pagination/meta objects.
            stack = [category]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    for k, v in cur.items():
                        lk = str(k).lower()
                        if "token" in lk and "next" in lk and v:
                            return str(v)
                        if isinstance(v, (dict, list, tuple)):
                            stack.append(v)
                elif isinstance(cur, (list, tuple)):
                    for item in cur:
                        if isinstance(item, (dict, list, tuple)):
                            stack.append(item)
            return None

        for _ in range(max_pages):
            result_specs = [
                {
                    "contentRestrictions": {
                        "allowedParentalControls": {"hasExplicitLanguage": True},
                        "assetQuality": {"quality": []},
                        "contentTier": "UNLIMITED" if region_to_use.country != "IN" else "PRIME",
                        "eligibility": None,
                    },
                    "documentSpecs": [
                        {
                            "fields": [
                                "__default",
                                "parentalControls.hasExplicitLanguage",
                                "contentTier",
                                "artOriginal",
                                "contentEncoding",
                            ],
                            "filters": None,
                            "type": label_type,
                        }
                    ],
                    "label": label_type,
                    "maxResults": page_size,
                    "pageToken": page_tokens.get(label_type),
                    "topHitSpec": None,
                }
                for label_type in search_types
            ]

            data = {
                "customerIdentity": {
                    "customerId": self.credentials.customer_id,
                    "deviceId": self.credentials.device_info.device_serial_number,
                    "deviceType": AmazonMobileApplication.MUSIC.device_type,
                    "musicRequestIdentityContextToken": None,
                    "sessionId": "123-1234567-5555555",  # this is legit what the app uses :skull:
                },
                "explain": None,
                "features": {
                    "spellCorrection": {
                        "accepted": None,
                        "allowCorrection": True,
                        "rejected": None,
                    },
                    "spiritual": None,  # a boolean, unknown purpose
                    "upsell": {"allowUpsellForCatalogContent": False},
                },
                "locale": region_to_use.locale,
                "musicTerritory": region_to_use.country,
                "query": query,
                "queryMetadata": None,
                "resultSpecs": result_specs,
            }

            response = self._music_api_call(
                "textsearch/search/v1_1",
                "com.amazon.tenzing.textsearch.v1_1.TenzingTextSearchServiceExternalV1_1.search",
                data,
                region_to_use=region_to_use,
            )
            resp_json = response.json()
            LOGGER.debug(resp_json)

            results = resp_json.get("results", {})
            if not results:
                break

            next_page_tokens = {}
            docs_added_this_page = 0
            for category in results:
                if not isinstance(category, dict):
                    continue
                label = str(category.get("label") or "")
                if label:
                    next_page_tokens[label] = _next_token_for_category(category)

                if int(category.get("totalHitCount", 0)) == 0:
                    continue
                for hit in category.get("hits", []):
                    document = dict(hit.get("document") or {})
                    if not document:
                        continue
                    dedupe_key = str(
                        document.get("asin")
                        or document.get("seriesAsin")
                        or document.get("artistAsin")
                        or document.get("albumAsin")
                        or ""
                    )
                    if dedupe_key and dedupe_key in seen_asins:
                        continue
                    if dedupe_key:
                        seen_asins.add(dedupe_key)
                    collected_docs.append(document)
                    docs_added_this_page += 1

            if asins:
                for asin in asins:
                    result = next(
                        (
                            doc
                            for doc in collected_docs
                            if str(asin)
                            in {
                                str(doc.get(item))
                                for item in ("albumAsin", "artistAsin", "asin", "seriesAsin")
                                if doc.get(item)
                            }
                        ),
                        None,
                    )
                    if result:
                        return result
            else:
                if len(collected_docs) >= requested_limit:
                    return tuple(collected_docs[:requested_limit])

            page_tokens = {
                label_type: next_page_tokens.get(label_type)
                for label_type in search_types
            }
            has_more_pages = any(page_tokens.values())
            if not has_more_pages or docs_added_this_page == 0:
                break

        if asins:
            return {}
        return tuple(collected_docs[:requested_limit]) if collected_docs else {}

    def get_catalog_playlist(self, asin: str, region_to_use: typing.Optional[AmazonRegion] = None):
        """Get a playlist and its tracks by ASIN."""
        resp = self._music_api_call(
            "playlists",
            "com.amazon.musicplaylist.model.MusicPlaylistService.getCatalogPlaylistByAsin",
            {
                "asin": asin,
                "contentEncoding": True,
                "customerInfo": {
                    "customerId": "",
                    "deviceId": self.credentials.device_info.device_serial_number,
                    "deviceType": AmazonMobileApplication.MUSIC.device_type,
                },
                "musicTerritory": (region_to_use or self.credentials.account_region).country,
            },
            region_to_use=region_to_use,
        )
        return dict(resp.json())

    def get_user_playlist(self, playlist_uuid: str):
        """Get a user playlist and its tracks by UUID."""
        resp = self._music_api_call(
            "playlists",
            "com.amazon.musicplaylist.model.MusicPlaylistService.getPlaylistsByIdV2",
            {
                "contentEncoding": True,
                "customerInfo": {
                    "customerId": "",
                    "deviceId": self.credentials.device_info.device_serial_number,
                    "deviceType": AmazonMobileApplication.MUSIC.device_type,
                },
                "featureSet": ["SUPPORT_MIXED_ID_TYPES", "INCLUDE_FOLLOWER_COUNT"],
                "playlistIds": [playlist_uuid],
                "requestedMetadata": (
                    "albumArtistAsin albumArtistName albumAsin albumContributors "
                    "albumCoverImageFull albumCoverImageLarge albumCoverImageMedium "
                    "albumCoverImageSmall albumCoverImageTiny albumCoverImageXL albumName "
                    "albumPrimaryGenre albumRating albumReleaseDate artistAsin artistName "
                    "asin assetType assetEligibility audioUpgradeDate bitrate composer "
                    "contributors creationDate customMeta discNum dmid duration eligibility "
                    "fileExtension fullAlbumPurchased gracenoteId instantImport "
                    "isMusicSubscription internalTags lastUpdatedDate localFilePath "
                    "lyricist marketplace matchType matchVersion md5 fileName objectId "
                    "orderId parentalControls performer physicalOrderId primaryGenre "
                    "primeStatus publisher purchased purchaseDate rating rogueBackfillDate "
                    "fileSize songWriter sortAlbumArtistName sortAlbumName sortArtistName "
                    "sortTitle status storageLocation title trackNum errorCode uploaded"
                ).split(),
            },
        )
        return dict(resp.json())

    def get_track_lyrics(self, track_asin: str, region_to_use: typing.Optional[AmazonRegion] = None) -> dict[str, typing.Any]:
        """
        Get the lyrics for a track.

        Returns a dict with `lrcSource`, `lyrics` (with `lines`, `writers`,
        `explicitLyricsStatus`), `lyricsResponseCode` ('1002' found, '2001' not),
        `lyricsSource` and `trackAsinAndMarketplace` keys.
        """
        if not region_to_use:
            region_to_use = self.credentials.account_region

        if region_to_use.region.name == "FE":
            tld = "co.jp"
        elif region_to_use.region.name == "NA":
            tld = "com"
        elif region_to_use.region.name == "EU":
            tld = "eu"
        else:
            print(
                "Warning! This type of TLD is not recognized, \n"
                "You are LIKELY to encounter an error. \n"
                f"URL: https://music-xray-service.amazon.{tld}/"
            )

        response = self.post(
            url=f"https://music-xray-service.amazon.{tld}/",
            headers={
                "User-Agent": self.APP_USER_AGENT,
                "x-amz-target": "com.amazon.musicxray.MusicXrayService.getLyricsByTrackAsinBatch",
                "x-amz-requestid": str(uuid.uuid4()),
            },
            data={
                "trackAsinsAndMarketplaceList": [
                    {
                        "asin": track_asin,
                        "musicTerritory": region_to_use.country,
                    }
                ]
            },
        )

        if response.status_code == 200:
            return dict(response.json().get("lyricsResponseList", [{}])[0])
        return {}

    def get_tracks_manifest(
        self, asins: typing.Iterable[str], force_3d: typing.Optional[bool] = None, region_to_use: typing.Optional[AmazonRegion] = None
    ):
        """
        Get the playback manifest of tracks (MPD)

        Args:
            asins: An iterable of str. They all must be a valid ASIN.
            force_3d: typing.Optional[bool]: Sometimes 3D audio isn't attributed to the ASIN.
            Setting this to true allows Amazon to subtitute the ASIN provided for another ASIN
            which has 3D audio (different ASIN, same metadata). A downside for enabling this option results in UHD not being provided.

        Returns:
            A generator which yields a tuple of the corresponding track ASIN and
            the Amazon Music Dash Manifest as a `xml.etree.ElementTree`

            TRACK_PSSH + SIREN_KATANA = All audio format (Lossless and 360).
            TRACK_PSSH + SIREN_KATANA_NO_CLEAR_LEAD = No issues, only up to lossless
        """
        if not region_to_use:
            region_to_use = self.credentials.account_region

        # Amazon only allows a specific amount of ASINs to be requested at once (10 asins)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(self._get_tracks_manifest, tuple(item), region_to_use, force_3d)
                for item in divide_sequence(list(asins), size=10)
            ]
            executor.shutdown(wait=True)
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if not result:
                    continue
                yield from self.parse_from_content_responses(result)

    def _get_tracks_manifest(
        self, asins: tuple[str], region_to_use: AmazonRegion, force_3d: typing.Optional[bool] = None
    ):
        """Internal function of get_tracks_manifest"""
        content_id_list = [
            {
                "identifier": asin,
                "identifierType": "ASIN",
            }
            for asin in asins
        ]
        music_agent = f"Harley/{self.harley_version} Harley/{self.application_version} ( {str(uuid.uuid4())} {asins[0]} )"
        response = self._music_api_call(
            "dmls/getDashManifestsV2",
            "com.amazon.digitalmusiclocator.DigitalMusicLocatorServiceExternal.getDashManifestsV2",
            {
                "appInfo": {"musicAgent": music_agent},
                "contentIdList": content_id_list,
                "contentProtectionList": [
                    "GROUP_PSSH",  # for entitlement key, mobile uses this
                    "TRACK_PSSH",  # used for web playback
                ],
                "customerInfo": {
                    "entitlementList": [
                        "NIGHTWING",
                        "SONIC_RUSH",
                        "HAWKFIRE",  # used in app
                        "ROBIN",
                        "KATANA",  # used in app
                        "MERCURY"
                    ],
                    "marketplaceId": region_to_use.marketplace_id,
                    "territoryId": region_to_use.country,
                },
                "customerId": self.credentials.customer_id,
                "deviceToken": {
                    "deviceId": self.credentials.device_info.device_serial_number,
                    "deviceTypeId": AmazonMobileApplication.MUSIC.device_type,
                },
                "musicDashVersionList": [
                    "SIREN_KATANA",  # with 360 audio
                ],
                # Sometimes having tryAsinSubstitution set to true
                # but no try3dAsinSubstitution
                # fails to get 360RA audio (3-6) for these albums:
                # https://music.amazon.co.jp/albums/B08P6QMJ9D?trackAsin=B08P6S83PK
                # https://music.amazon.ca/albums/B08P688B62
                # Having both Asin and 3dAsin substitution
                # has 360RA spatial audio, but no UHD
                "try3dAsinSubstitution": True if force_3d else False,
                "tryAsinSubstitution": True,
            },
            region_to_use=region_to_use,
            user_agent=self.HARLEY_USER_AGENT,
            extra_headers={"Accept": "application/json, text/javascript, */*"},
        )
        resp_dict = response.json()

        if (
            response.status_code != 200
            or resp_dict["contentResponseList"][0]["contentResponseStatusCode"]
            != "SUCCESS"
        ):
            raise Exception(
                f"Failed to get track manifest: {response.status_code} {response.text}"
            )

        result: list[dict] = resp_dict.get("contentResponseList", [])
        return result

    def get_license_response(self, asin: str, challenge: str, drm_type: typing.Optional[str] = "WIDEVINE") -> str:
        """
        Retrieve a License Response with a License Challenge.

        Args:
            asin: The ASIN of the item.
            challenge: A base64 encoded Widevine challenge.

        Returns:
            The response from the license server.

        Valid DRM types:

        `WIDEVINE_ENTITLEMENT`, `PLAYREADY`, `FAIRPLAY`, `WIDEVINE`

        Entitlement is not possible without the proper widevine device, 9480
        """
        response = self._music_api_call(
            "dmls/getLicenseForPlaybackV2",
            "com.amazon.digitalmusiclocator.DigitalMusicLocatorServiceExternal.getLicenseForPlaybackV2",
            {
                "DrmType": str(drm_type),
                "appInfo": {
                    "musicAgent": f"Harley/{self.harley_version} Harley/{self.application_version} ( {str(uuid.uuid4())} {asin} )"
                },
                "deviceToken": {
                    "deviceId": self.credentials.device_info.device_serial_number,
                    "deviceTypeId": AmazonMobileApplication.MUSIC.device_type,
                },
                "licenseChallenge": challenge,
                "persistent": False,
            },
            user_agent=self.USER_AGENT,
            extra_headers={
                "Origin": f"https://music.amazon.{self.credentials.account_region.domain_tld}",
                "Referer": f"https://music.amazon.{self.credentials.account_region.domain_tld}/",
            },
        )

        if response.status_code != 200:
            raise ValueError(
                f"Failed to get license: {response.status_code} {response.text}"
            )
        resp = response.json()
        return resp["license"]

    # Shortcuts

    def get_track_manifest(
        self, track_asin: str, *args, **kwargs
    ):
        return next(
            self.get_tracks_manifest((track_asin,), *args, **kwargs),
            (None, None),
        )

    def get_info(self, asin: str, list_key: str, *args, **kwargs):
        """Get the single metadata entry for a track/album/artist ASIN
        (`list_key` is 'trackList', 'albumList' or 'artistList')."""
        kind = {"trackList": "Track", "albumList": "Album", "artistList": "Artist"}[list_key]
        resp = self.get_metadata(asin, *args, **kwargs)[list_key]
        if len(resp) > 1 or not resp:
            raise ValueError(f"{kind} metadata is {'not available' if not resp else 'invalid'}: {resp}")
        return resp[0]

    def get_track_xray(self, asin: str, region_to_use: AmazonRegion, parse_credits: typing.Optional[bool] = False):
        response = self.post(
            url=f"https://{str(self.credentials.account_region.region.name).lower()}.mobilemesk.skill.music.a2z.com/api/showXray/{asin}",
            add_default_stratus_headers=False,
            headers={
                "x-amzn-device-id": self.credentials.device_info.device_serial_number,
                "x-amzn-device-family": "MobileAndroid",
                "x-amzn-device-manufacturer": "Google",
                "x-amzn-device-model": "Pixel 5",
                "x-amzn-device-language": region_to_use.locale,
                "x-amzn-device-height": "2560",
                "x-amzn-device-width": "1440",
                "x-amzn-device-scale": "3.5",
                "x-amzn-application-version": self.application_version,
                "x-amzn-os-version": "11",
                "x-amzn-device-time-zone": "America/Toronto",
                "x-amzn-timestamp": f"{time.time_ns() // 1_000_000}",
                "x-amzn-user-agent": self.APP_USER_AGENT,
                "x-amzn-device-type-id": AmazonMobileApplication.MUSIC.device_type,
                "x-amzn-request-id": str(uuid.uuid4()).lower(),
                "x-amzn-authentication": json.dumps(
                    {
                        "interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement",
                        "accessToken": f"{self.credentials.access_token}",
                    }
                ),
                "x-amzn-session-id": self.credentials.website_cookies["session-id"],
                "content-type": "application/json; charset=utf-8",
                "accept-encoding": "gzip",
                "user-agent": "okhttp/4.10.0",
            },
            data={
                "assetType": "AUDIO",
                "swipeablePageConfig": json.dumps(
                    {
                        "interface": "Touch.SwipeablePagesTemplateInterface.v1_0.SwipeablePagesClientInformation",
                        "isChartsV3Enabled": True,
                        "isStageEnabled": False,
                    }
                ),
            },
        )

        resp_dict = dict(response.json())

        if parse_credits:
            return self.parse_credits_from_xray(resp_dict)

        return resp_dict

    @staticmethod
    def parse_credits_from_xray(response: dict):
        credits_mapping: dict[str, list[str]] = {}
        for method in response.get("methods", []):
            if not str(method.get("interface", "")).endswith(
                "CreateAndBindManagedContainerMethod"
            ):
                continue
            for page in method.get("template", {}).get("pages", []):
                if not str(page.get("interface", "")).endswith("ScrollableListElement"):
                    continue
                if str(page.get("label", {}).get("title")) != "CREDITS":
                    continue

                for page_element in page.get("elements", []):
                    if not str(page_element.get("interface", "")).endswith(
                        "VerticalContainerElement"
                    ):
                        continue
                    credit_name: str = ""
                    people_names: list[str] = []

                    for container_element in page_element.get("elements", []):
                        if str(container_element.get("interface", "")).endswith(
                            "LabelElement"
                        ):
                            raw_credit_name = str(
                                "".join(
                                    re.findall(r"[A-Z][^A-Z]*", container_element["text"])
                                )
                            ).title()
                            credit_name = CREDIT_NAME_FIXES.get(
                                raw_credit_name, raw_credit_name
                            )

                        if str(container_element.get("interface", "")).endswith(
                            "ClickableTextElement"
                        ):
                            people_names.append(container_element["text"])

                    if not (credit_name and people_names):
                        continue

                    names = credits_mapping.get(credit_name, [])
                    names.extend(people_names)
                    # Remove duplicate names
                    names = sorted(
                        set(names),
                        key=names.index
                    )

                    credits_mapping.update({credit_name: names})

        return credits_mapping

    @staticmethod
    def parse_from_content_responses(content_responses: list[dict[str, typing.Any]]):
        for content_response in content_responses:
            content_identifier = content_response.get("contentIdentifier", {})
            if not (content_identifier or isinstance(content_identifier, dict)):
                raise ValueError(type(content_identifier))

            if content_identifier.get("identifierType") != "ASIN":
                raise ValueError(
                    f"{content_identifier.get('identifierType')} is not an ASIN!"
                )
            asin = str(content_identifier.get("identifier", ""))

            manifest = None
            if content_response.get("contentResponseStatusCode") == "SUCCESS":
                manifest = ElementTree.fromstring(
                    re.sub(
                        r'xmlns="[^"]+"',
                        "",
                        content_response.get("manifest", ""),
                        count=1,
                    )
                )

            yield asin, manifest

    @staticmethod
    def _create_httpx_session():
        default_headers = {
            "User-Agent": AmazonMusicMobileAPI.USER_AGENT,
            "Accept-Language": "en-US",
            "Accept-Encoding": "gzip",
            "x-requested-with": "com.amazon.mp3",
        }

        session = httpx.Client(
            headers=default_headers,
            follow_redirects=True,
        )
        return session

    @classmethod
    def register(
        cls,
        application: AmazonMobileApplication,
        selected_region: AmazonRegion,
        authorization_code: str,
        code_verifier: bytes,
        serial: str,
    ):
        """Registers a dummy Amazon device for Amazon Music and returns an
        instance of AmazonMusicMobileAPI with the credentials attached."""

        device_name = f"ripperino {os.urandom(16).hex()} Android Device (MP3)"
        LOGGER.debug(f"Registering device {device_name} with serial {serial}")

        body = {
            "requested_token_type": [
                "bearer",
                "mac_dms",
                "website_cookies",
                "store_authentication_cookie",
            ],
            "cookies": {"website_cookies": [], "domain": f".amazon.{selected_region.domain_tld}"},
            "registration_data": {
                "domain": "Device",
                "app_version": cls.application_version,
                "device_serial": serial,
                "device_type": application.device_type,
                "device_name": device_name,
                "os_version": "11",
                "software_version": "523160014",
                "device_model": "Pixel 5",
                "app_name": application.official_name,
            },
            "auth_data": {
                "client_id": cls._build_client_id(serial, application),
                "authorization_code": authorization_code,
                "code_verifier": code_verifier.decode(),
                "code_algorithm": "SHA-256",
                "client_domain": "DeviceLegacy",
            },
            "requested_extensions": ["device_info", "customer_info"],
        }

        resp = httpx.post(f"https://api.amazon.{selected_region.domain_tld}/auth/register", json=body)

        LOGGER.debug(json.dumps(resp.json(), indent=4))
        resp_json = resp.json()
        if resp.status_code != 200:
            raise ValueError(resp_json)

        success_response = resp_json["response"]["success"]

        tokens = dict(success_response["tokens"])
        adp_token = tokens["mac_dms"]["adp_token"]
        device_private_key = tokens["mac_dms"]["device_private_key"]
        pem_prefix = "-----BEGIN RSA PRIVATE KEY-----\n"
        pem_suffix = "\n-----END RSA PRIVATE KEY-----"
        if not str(device_private_key).startswith(
            pem_prefix
        ) and not str(device_private_key).endswith(pem_suffix):
            key = RSA.import_key(base64.b64decode(str(device_private_key)))
            device_private_key = rsa.PrivateKey.load_pkcs1(key.export_key("PEM"))
        else:
            key = rsa.PrivateKey.load_pkcs1(device_private_key)

        store_authentication_cookie = tokens["store_authentication_cookie"]
        access_token = tokens["bearer"]["access_token"]
        refresh_token = tokens["bearer"]["refresh_token"]
        expires_s = int(tokens["bearer"]["expires_in"])
        expires = datetime.utcnow() + timedelta(seconds=expires_s)

        extensions = success_response["extensions"]
        device_info = AmazonMusicDevice(**dict(extensions["device_info"]))
        customer_info = dict(extensions["customer_info"])

        website_cookies = {
            cookie["Name"]: str(cookie["Value"]).replace(r'"', r"")
            for cookie in tokens.get("website_cookies", [{}])
        }

        credentials = AmazonMusicMobileAPICredentials(
            adp_token=adp_token,
            device_private_key=device_private_key,
            access_token=access_token,
            refresh_token=refresh_token,
            expires=expires,
            website_cookies=website_cookies,
            store_authentication_cookie=store_authentication_cookie,
            device_info=device_info,
            customer_info=customer_info,
        )

        return cls(credentials)

    @staticmethod
    def _build_client_id(
        serial: str, app: AmazonMobileApplication
    ) -> str:
        client_id = serial.encode() + f"#{app.device_type}".encode("utf-8")
        return client_id.hex()

    @staticmethod
    def _build_init_cookies() -> dict[str, str]:
        """Build initial cookies to prevent captcha in most cases."""

        frc = secrets.token_bytes(313)
        frc = base64.b64encode(frc).decode("ascii").rstrip("=")
        amzn_app_id = "MAPAndroidLib-1.3.4028.0"

        map_md = {
            "device_registration_data": {"software_version": "130050002"},
            "app_identifier": {
                "package": "com.amazon.mp3",
                "SHA-256": [
                    "2f19adeb284eb36f7f07786152b9a1d14b21653203ad0b04ebbf9c73ab6d7625"
                ],
                # https://www.apkmirror.com/apk/amazon-mobile-llc/amazon-music-discover-songs/amazon-music-discover-songs-22-15-12-release/amazon-music-songs-podcasts-22-15-12-4-android-apk-download/
                "app_version": "523160014",
                "app_version_name": AmazonMusicMobileAPI.application_version,
                "app_sms_hash": "QGCBba+brC5",
                "map_version": amzn_app_id,
            },
            "app_info": {
                "auto_pv": 0,
                "auto_pv_with_smsretriever": 0,
                "smartlock_supported": 0,
                "permission_runtime_grant": 2,
            },
            "device_user_dictionary": [],  # maybe adding the email would help bypass captcha
        }

        map_md = json.dumps(map_md)
        map_md = base64.b64encode(map_md.encode()).decode().rstrip("=")

        return {"frc": frc, "map-md": map_md, "amzn-app-id": amzn_app_id}

    @staticmethod
    def _build_oauth_url(
        domain: str,
        code_verifier: bytes,
        application: AmazonMobileApplication,
        selected_region: AmazonRegion,
        serial: typing.Optional[str] = None,
    ) -> tuple[str, str]:
        """Builds the url to login to Amazon Music."""

        serial = (
            serial or "PIXEL5" + build_device_serial()
        )  # requires some random model name at the start
        client_id = AmazonMusicMobileAPI._build_client_id(serial, application)
        code_challenge = create_s256_code_challenge(code_verifier)

        LOGGER.debug("device serial: %s", serial)
        LOGGER.debug("client id: %s", client_id)

        base_url = f"https://www.amazon.{domain}/ap/signin"
        return_to = f"https://www.amazon.{domain}/ap/maplanding"

        oauth_params = {
            "openid.pape.max_auth_age": "0",
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "accountStatusPolicy": "P1",
            "language": selected_region.locale,
            "openid.return_to": return_to,
            "openid.assoc_handle": application.assoc_handle,
            "openid.oa2.response_type": "code",
            "openid.mode": "checkid_setup",
            "openid.ns.pape": "http://specs.openid.net/extensions/pape/1.0",
            "openid.oa2.code_challenge_method": "S256",
            "openid.ns.oa2": f"http://www.amazon.{domain}/ap/ext/oauth/2",
            "openid.oa2.code_challenge": code_challenge,
            "openid.oa2.scope": "device_auth_access",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.oa2.client_id": f"device:{client_id}",
            "disableLoginPrepopulate": "0",
            "openid.ns": "http://specs.openid.net/auth/2.0",
            "forceMobileLayout": "true",  # custom, unsure if required by azm or is useless
        }
        if (
            selected_region.region is not AmazonContinent.NA
            and selected_region.country not in ("AU")
        ):
            # TODO, find which countries that require to login into prime video first
            # NOTE: amz music australia hates the marketplace id in the oauth url (404)
            oauth_params.update({"marketPlaceId": selected_region.marketplace_id})

        return f"{base_url}?{urlencode(oauth_params)}", serial

    def refresh_access_token(self, force: bool = False) -> None:
        """Refresh the access token"""
        if force or self.credentials.access_token_expired:
            if self.credentials.refresh_token is None:
                message = "No refresh token found. Can't refresh access token."
                LOGGER.critical(message)
                raise Exception(message)

            body = {
                "app_name": "Amazon Music",
                "app_version": self.application_version,
                "source_token": self.credentials.refresh_token,
                "requested_token_type": "access_token",
                "source_token_type": "refresh_token",
            }

            resp = self.post(
                f"https://api.amazon.{self.credentials.account_region.domain_tld}/auth/token",
                data=body,
                sign=False,
            )
            resp_dict = resp.json()
            resp.raise_for_status()

            expires = datetime.utcnow() + timedelta(
                seconds=int(resp_dict["expires_in"])
            )

            self.credentials.access_token = resp_dict["access_token"]
            self.credentials.expires = expires

        else:
            LOGGER.info(
                "Access Token not expired. No refresh necessary. "
                "To force refresh please use force=True"
            )

    def _apply_signing_auth_flow(self, request: httpx.Request) -> None:
        date = datetime.utcnow().isoformat("T") + "Z"
        body = request.content.decode("utf-8")

        data = f"{request.method}\n{request.url.raw_path.decode()}\n{date}\n{body}\n{self.credentials.adp_token}"

        cipher = rsa.pkcs1.sign(data.encode(), self.credentials.device_private_key, "SHA-256")
        signed_encoded = base64.b64encode(cipher)

        signature = f"{signed_encoded.decode()}:{date}"

        headers = {
            "x-adp-token": self.credentials.adp_token,
            "x-adp-alg": "SHA256withRSA:1.0",
            "x-adp-signature": signature,
        }

        request.headers.update(headers)
        LOGGER.debug("Signing auth flow applied to request")

    def _list_devices(self):
        devices_resp = self._music_api_call(
            "stratus",
            "com.amazon.stratus.StratusServiceExternal.listDevicesByCustomerId",
            {
                "customerId": None,
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": self.credentials.device_info.device_type,
            },
        )
        LOGGER.debug(
            f"{devices_resp.status_code} {json.dumps(devices_resp.json(), indent=4)}"
        )
        return devices_resp

    def authorize_device(self, device_serial: typing.Optional[str] = None):
        device_type = AmazonMobileApplication.MUSIC.device_type

        if not device_serial:
            device_serial = self.credentials.device_info.device_serial_number

        auth_device_resp = self._music_api_call(
            "stratus",
            "com.amazon.stratus.StratusServiceExternal.authorizeDevice",
            {
                "capabilities": [
                    "RETRIEVE_OWNED_CONTENT",
                    "RETRIEVE_ROBIN_CONTENT",
                ],
                "customerInfo": {
                    "customerId": "",  # it is not set, but it is required
                    "deviceId": device_serial,
                    "deviceType": device_type,
                },
                "deviceId": device_serial,
                "deviceType": device_type,
                "targetDeviceId": device_serial,
                "targetDeviceType": device_type,
            },
        )
        LOGGER.debug(auth_device_resp.content)
        LOGGER.debug(
            f"{auth_device_resp.status_code} {json.dumps(auth_device_resp.json(), indent=4)}"
        )
        return auth_device_resp

    def retrieve_capability(self):
        response = self._music_api_call(
            "stratus",
            "com.amazon.stratus.StratusServiceExternal.retrieveCapability",
            {
                "capabilityTypes": [
                    "RETRIEVE_ROBIN_CONTENT",
                ],
                "customerId": self.credentials.customer_id,
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": AmazonMobileApplication.MUSIC.device_type,
            },
        )
        return dict(response.json())

    @functools.lru_cache()
    def get_account_status(self):
        response = self.post(
            url=f"https://music.amazon.com/{self.credentials.customer_info['home_region']}/api/stratus/",
            headers={
                "x-amz-target": "com.amazon.stratus.StratusServiceExternal.isAccountValid",
                "x-amz-requestid": str(uuid.uuid4()),
            },
            data={
                "customerId": self.credentials.customer_id,
                "deviceId": self.credentials.device_info.device_serial_number,
                "deviceType": AmazonMobileApplication.MUSIC.device_type,
                "ipAddress": None,
                "verbose": True,
            },
        )
        return dict(response.json())

    def get_account_subscription_tier(self, resp: typing.Optional[dict] = None):
        if not resp:
            resp = self.get_account_status()

        customer_benefits = resp.get("customerAccount", {}).get("customerBenefits", {})
        if customer_benefits.get("HAWKFIRE_KATANA_ACCESS") == "true" and customer_benefits.get("HAWKFIRE_PLAYBACK_ACCESS") == "true":
            return AmazonMusicTier.UNLIMITED
        elif customer_benefits.get("PRIME_MUSIC_BROWSE") == "true" and customer_benefits.get("PRIME_MUSIC_CONTENT_ACCESS") == "true":
            return AmazonMusicTier.PRIME
        return AmazonMusicTier.FREE

    @staticmethod
    def _external_login(
        oauth_url: str,
        application: AmazonMobileApplication,
        oauth_flow_callback: typing.Optional[typing.Callable[[str, str], str]] = None,
    ):
        if oauth_flow_callback:
            callback_url = oauth_flow_callback(oauth_url, application.official_name)
        else:
            print(
                "\n"
                "=== Amazon Music login (browser) ===\n"
                "\n"
                "1. Open this URL in your browser (Ctrl+click if your terminal supports it):\n"
                f"\n{oauth_url}\n"
                "\n"
                "2. Sign in with your Amazon account.\n"
                "   You may need to enter your password twice and complete a CAPTCHA.\n"
                "\n"
                "3. After login, the browser will show a \"not found\" / error page — that is normal.\n"
                "\n"
                "4. Copy the full URL from the address bar and paste it below.\n"
                f"\n"
                f"   (Logging into {application.official_name} as required by the module.)\n"
            )
            callback_url = input("\nPaste the URL from your browser after login:\n").strip()

        if not callback_url or not str(callback_url).strip():
            raise ValueError("Amazon Music login cancelled: no callback URL provided.")

        response_url = httpx.URL(str(callback_url).strip())
        parsed_url = parse_qs(response_url.query.decode())

        if "openid.oa2.authorization_code" not in parsed_url:
            raise ValueError(
                "Amazon Music login failed: pasted URL does not contain an authorization code.\n"
                "Copy the full address bar URL from the page shown right after login (maplanding)."
            )

        authorization_code = parsed_url["openid.oa2.authorization_code"][0]
        return authorization_code


T = typing.TypeVar("T")


def divide_sequence(seq: typing.Sequence[T], size: int) -> typing.Generator[typing.Sequence[T], None, None]:
    """Divide a sequence into chunks of `size`."""
    for index in range(0, len(seq), size):
        yield seq[index : index + size]
