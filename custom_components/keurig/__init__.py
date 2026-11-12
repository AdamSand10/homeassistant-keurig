"""The Keurig Connect integration."""
from __future__ import annotations
import asyncio
from io import BytesIO
from aiohttp import web
from PIL import Image
from httpx import HTTPStatusError

from homeassistant.components.http.view import HomeAssistantView
from homeassistant.helpers import device_registry, entity_registry
from .helpers import get_brewers_by_entity_id, get_brewers_for_service

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    ATTR_ID,
    ATTR_NAME,
    ATTR_TEMPERATURE,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .pykeurig.keurigapi import KeurigApi, UnauthorizedException

from .const import (
    CONF_REFRESH_TOKEN,
    ATTR_POD_BRAND,
    ATTR_POD_VARIETY,
    ATTR_SIZE,
    DOMAIN,
    SERVICE_ADD_FAVORITE,
    SERVICE_BREW_FAVORITE,
    SERVICE_BREW_HOT_WATER,
    SERVICE_BREW_HOT,
    ATTR_INTENSITY,
    SERVICE_BREW_ICED,
    SERVICE_BREW_RECOMMENDATION,
    SERVICE_CANCEL_BREW,
    SERVICE_DELETE_FAVORITE,
    SERVICE_UPDATE_FAVORITE,
)
import logging
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Keurig Connect from a config entry."""

    hass.data.setdefault(DOMAIN, {})
    refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
    if not refresh_token:
        # Entries from the old password login have no refresh token
        raise ConfigEntryAuthFailed("Sign in to Keurig again (password login was retired)")

    def _save_refresh_token(new_token: str) -> None:
        # Auth0 rotates refresh tokens; the previous one is dead once replaced
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: new_token}
        )

    client = KeurigApi(
        refresh_token=refresh_token, token_update_callback=_save_refresh_token
    )
    try:
        await client.async_ensure_token()
    except UnauthorizedException as err:
        raise ConfigEntryAuthFailed("Keurig session expired; sign in again") from err
    except Exception as err:
        # Network / 5xx / Cloudflare: let HA retry setup with its own backoff
        raise ConfigEntryNotReady(f"Keurig authentication failed: {err}") from err

    coordinator = KeurigCoordinator(hass, client, entry)

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_config_entry_first_refresh()

    async def _target_devices(call: ServiceCall):
        """Resolve the brewers targeted by a service call."""
        matched = get_brewers_for_service(
            hass,
            call.data.get(ATTR_AREA_ID),
            call.data.get(ATTR_DEVICE_ID),
            call.data.get(ATTR_ENTITY_ID),
        )
        devices = [d for d in (await coordinator.get_devices() or []) if d.id in matched]
        if not devices:
            raise ServiceValidationError("No Keurig brewer matched the service target")
        return devices

    async def _run(action, *, check_result=False):
        """Run an API call, handling reauth and surfacing refusals."""
        try:
            result = await action()
        except UnauthorizedException:
            entry.async_start_reauth(hass)
            raise HomeAssistantError("Keurig authorization failed; reauth started")
        except HTTPStatusError as err:
            raise HomeAssistantError(
                f"Keurig API error {err.response.status_code}: {err.response.text}"
            ) from err
        return result

    def _refused(device):
        return HomeAssistantError(
            f"{device.name} not ready to brew "
            f"(brewer={device.brewer_status}, pod={device.pod_status}, "
            f"errors={device.errors})"
        )
    async def _brew(call: ServiceCall, method):
        for device in await _target_devices(call):
            # pykeurig returns False when the pre-brew status check fails
            if await _run(lambda d=device: method(d)) is False:
                raise _refused(device)
        await coordinator.async_request_refresh()

    async def handle_brew_hot_water(call: ServiceCall):
        size, temp = int(call.data[ATTR_SIZE]), int(call.data[ATTR_TEMPERATURE])
        await _brew(call, lambda d: d.hot_water(size, temp))

    async def handle_brew_hot(call: ServiceCall):
        size, temp = int(call.data[ATTR_SIZE]), int(call.data[ATTR_TEMPERATURE])
        intensity = int(call.data[ATTR_INTENSITY])
        await _brew(call, lambda d: d.brew_hot(size, temp, intensity))

    async def handle_brew_iced(call: ServiceCall):
        await _brew(call, lambda d: d.brew_iced())

    async def handle_brew_recommendation(call: ServiceCall):
        size = int(call.data[ATTR_SIZE])
        await _brew(call, lambda d: d.brew_recommendation(size))

    async def handle_brew_favorite(call: ServiceCall):
        favorite_id = call.data[ATTR_ID]
        await _brew(call, lambda d: d.brew_favorite(favorite_id))

    async def handle_cancel_brew(call: ServiceCall):
        for device in await _target_devices(call):
            if await _run(device.cancel_brew) is False:
                raise HomeAssistantError(f"Cancel brew failed for {device.name}")
        await coordinator.async_request_refresh()
 
    # Favorites belong to the Keurig account, not a specific brewer

    async def handle_add_favorite(call: ServiceCall):
        await _run(
            lambda: client.async_add_favorite(
                call.data[ATTR_NAME],
                int(call.data[ATTR_SIZE]),
                int(call.data[ATTR_TEMPERATURE]),
                int(call.data[ATTR_INTENSITY]),
            )
        )

    async def handle_update_favorite(call: ServiceCall):
        await _run(
            lambda: client.async_update_favorite(
                call.data[ATTR_ID],
                call.data[ATTR_NAME],
                int(call.data[ATTR_SIZE]),
                int(call.data[ATTR_TEMPERATURE]),
                int(call.data[ATTR_INTENSITY]),
            )
        )
    async def handle_delete_favorite(call: ServiceCall):
        await _run(lambda: client.async_delete_favorite(call.data[ATTR_ID]))

    for name, handler in (
        (SERVICE_BREW_HOT_WATER, handle_brew_hot_water),
        (SERVICE_BREW_HOT, handle_brew_hot),
        (SERVICE_BREW_ICED, handle_brew_iced),
        (SERVICE_BREW_RECOMMENDATION, handle_brew_recommendation),
        (SERVICE_BREW_FAVORITE, handle_brew_favorite),
        (SERVICE_CANCEL_BREW, handle_cancel_brew),
        (SERVICE_ADD_FAVORITE, handle_add_favorite),
        (SERVICE_UPDATE_FAVORITE, handle_update_favorite),
        (SERVICE_DELETE_FAVORITE, handle_delete_favorite),
    ):
        hass.services.async_register(DOMAIN, name, handler)

    # Views can only be registered once per HA run; reloading the entry must not re-register them.
    if not hass.data.get(f"{DOMAIN}_views_registered"):
        hass.http.register_view(ApiBrandView(hass, coordinator, client))
        hass.http.register_view(ApiVarietyView(hass, coordinator, client))
        hass.data[f"{DOMAIN}_views_registered"] = True

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await hass.async_add_executor_job(hass.data[DOMAIN][entry.entry_id].api.disconnect)
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class KeurigCoordinator(DataUpdateCoordinator):
    """My custom coordinator."""

    def __init__(self, hass, api, entry, devices=None):
        """Initialize my coordinator."""
        super().__init__(
            hass, _LOGGER, name="Keurig", config_entry=entry,
            update_interval=timedelta(seconds=60),
        )
        self.api = api
        self.hass = hass
        self.entry = entry
        self._devices = None
        self._device_lock = asyncio.Lock()

    async def get_devices(self):
        async with self._device_lock:
            if self._devices is None:
                try:
                    self._devices = await self.api.async_get_devices()
                except UnauthorizedException:
                    self.entry.async_start_reauth(self.hass)
            return self._devices

    async def _async_update_data(self):
        # Push (SignalR) is intentionally not used: polling is enough, and its
        # background thread refreshed tokens concurrently with this coordinator.
        try:
            await self.api.async_ensure_token()
        except UnauthorizedException as err:
            raise ConfigEntryAuthFailed("Keurig rejected the stored credentials") from err
        except Exception as err:
            raise UpdateFailed(f"Keurig authentication unavailable: {err}") from err

        for device in await self.get_devices() or []:
            await device.async_update()


class KeurigView(HomeAssistantView):
    def __init__(self, hass, coordinator, api):
        """Initialize."""
        self.requires_auth = False
        self._api = api
        self.hass = hass
        self._coordinator = coordinator

    async def _get_device_by_entity_id(self, entity_id):
        device_ids = get_brewers_by_entity_id(
            self.hass,
            device_registry.async_get(self.hass),
            entity_registry.async_get(self.hass),
            [entity_id],
            [],
        )
        if len(device_ids) > 0:
            return next(
                (
                    dev
                    for dev in await self._coordinator.get_devices()
                    if dev.id == device_ids[0]
                )
            )
        else:
            return None


class ApiBrandView(KeurigView):
    def __init__(self, hass, coordinator, api):
        """Initialize."""
        self.url = "/api/keurig_brand_proxy/{entity_id}"
        self.name = "api:keurig:brand"
        super().__init__(hass, coordinator, api)

    async def get(self, request, entity_id: str):  # pylint: disable=unused-argument
        """Handle HACS Web requests."""

        device = await self._get_device_by_entity_id(entity_id)

        if device is None:
            return web.Response(status=404)

        if ATTR_POD_BRAND not in self.hass.states.get(entity_id).attributes:
            return web.Response(status=400)

        brand_id = device.pod_brand_id
        if brand_id is None:
            img = Image.new(mode="RGBA", size=(470, 320))
            default_image_stream = BytesIO()
            img.save(default_image_stream, "PNG")
            return web.Response(
                body=default_image_stream.getvalue(), content_type="image/png"
            )
        image_bytes = None
        try:
            image_bytes = await self._api.async_get_brand_image(brand_id)
        except HTTPStatusError as err:
            return web.Response(status=err.response.status_code)
        stream = BytesIO(image_bytes)

        return web.Response(body=stream.getvalue(), content_type="image/png")


class ApiVarietyView(KeurigView):
    def __init__(self, hass, coordinator, api):
        """Initialize."""
        self.url = "/api/keurig_variety_proxy/{entity_id}"
        self.name = "api:keurig:variety"

        super().__init__(hass, coordinator, api)

    async def get(self, request, entity_id: str):  # pylint: disable=unused-argument
        """Handle HACS Web requests."""

        device = await self._get_device_by_entity_id(entity_id)

        if device is None:
            return web.Response(status=404)

        if ATTR_POD_VARIETY not in self.hass.states.get(entity_id).attributes:
            return web.Response(status=400)

        variety_id = device.pod_variety_id
        if variety_id is None:
            img = Image.new(mode="RGBA", size=(2000, 2000))
            default_image_stream = BytesIO()
            img.save(default_image_stream, "PNG")
            return web.Response(
                body=default_image_stream.getvalue(), content_type="image/png"
            )
        image_bytes = None
        try:
            image_bytes = await self._api.async_get_variety_image(variety_id)
        except HTTPStatusError as err:
            return web.Response(status=err.response.status_code)
        stream = BytesIO(image_bytes)

        return web.Response(body=stream.getvalue(), content_type="image/png")
