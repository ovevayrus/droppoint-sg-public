"""Configuration for DropPoint SG.

Two layers:

- ``Settings`` reads everything from the environment (with a local ``.env`` file),
  including secrets. Use ``get_settings()`` to read it.
- ``OptimiserConfig`` is the small, secret-free bundle of tuning knobs that the pure
  optimiser functions take, so they never touch the environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load a local .env into the process environment if present. pydantic-settings also
# reads .env directly; this keeps plain os.environ lookups working too.
load_dotenv()

Objective = Literal["efficient", "balanced"]


class OptimiserConfig(BaseModel):
    """Tuning knobs for the optimiser. No secrets here."""

    max_driver_overhead_frac: float = 0.5
    require_fair_proportion: bool = True
    objective: Objective = "efficient"
    max_candidates_evaluated: int = 15
    corridor_max_offset_km: float = 3.0
    show_near_miss: bool = False

    # --- Multi-drop (2-3 passengers, one shared route) ---
    # No hard driver-overhead cap by default: plans are ranked by net time saved and
    # the driver's extra minutes are always shown. Set a fraction to restore a cap.
    multi_max_driver_overhead_frac: float | None = None
    multi_route_candidates: int = 4  # stations per rider from the A->B corridor
    multi_dest_candidates: int = 2  # stations per rider toward their own destination
    multi_corridor_max_offset_km: float = 5.0
    multi_num_options: int = 4
    multi_verify_top: int = 8  # plans re-costed with real inter-stop drive times
    inter_stop_speed_kmh: float = 28.0  # haversine estimate speed for pre-ranking
    stop_overhead_min: float = 1.5  # minutes lost pulling over per stop

    # --- Pick-up convenience meets ---
    # Pick-up mode exists to collect people, so meeting points that cost the group a
    # few minutes are still offered when they spare the driver a big detour. A meet
    # may leave a rider up to `pickup_rider_tolerance_min` slower than their own PT
    # trip, and must cut their PT leg to at most `pickup_pt_leg_max_frac` of it
    # (otherwise they are doing their whole trip themselves anyway).
    pickup_rider_tolerance_min: float = 5.0
    pickup_pt_leg_max_frac: float = 0.8
    pickup_spread_candidates: int = 8  # stations nominated along the rider's corridor


class Settings(BaseSettings):
    """All configuration, read from the environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Optimiser tuning (mirrors OptimiserConfig; overridable by env) ---
    max_driver_overhead_frac: float = 0.5
    require_fair_proportion: bool = True
    objective: Objective = "efficient"
    max_candidates_evaluated: int = 15
    corridor_max_offset_km: float = 3.0
    default_depart: str = "now"
    show_near_miss: bool = False
    multi_max_driver_overhead_frac: float | None = None
    multi_route_candidates: int = 4
    multi_dest_candidates: int = 2
    multi_corridor_max_offset_km: float = 5.0
    multi_num_options: int = 4
    multi_verify_top: int = 8
    inter_stop_speed_kmh: float = 28.0
    stop_overhead_min: float = 1.5
    pickup_rider_tolerance_min: float = 5.0
    pickup_pt_leg_max_frac: float = 0.8
    pickup_spread_candidates: int = 8

    # --- OneMap public-transport routing parameters ---
    pt_max_walk_distance_m: int = 1000
    pt_num_itineraries: int = 3

    # Single-process safeguards, not claims about the provider's account quota.
    max_trip_requests: int = Field(default=300, ge=1, le=10000)
    trip_work_seconds: float = Field(default=180.0, gt=0, le=900)
    trips_per_minute: int = Field(default=20, ge=1, le=1000)
    user_cooldown_seconds: float = Field(default=60.0, gt=0, le=3600)
    onemap_requests_per_minute: int = Field(default=600, ge=1, le=10000)
    allow_webhook_reset: bool = False

    # --- Secrets (never logged, never committed) ---
    onemap_token: SecretStr | None = None
    onemap_email: SecretStr | None = None
    onemap_password: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None

    @staticmethod
    def _secret_value(value: SecretStr | None) -> str | None:
        """Reveal a credential only at the boundary where it is required."""
        if value is None:
            return None
        revealed = value.get_secret_value()
        return revealed or None

    @property
    def onemap_token_value(self) -> str | None:
        return self._secret_value(self.onemap_token)

    @property
    def onemap_email_value(self) -> str | None:
        return self._secret_value(self.onemap_email)

    @property
    def onemap_password_value(self) -> str | None:
        return self._secret_value(self.onemap_password)

    @property
    def telegram_bot_token_value(self) -> str | None:
        return self._secret_value(self.telegram_bot_token)

    def optimiser(self) -> OptimiserConfig:
        """Return the secret-free optimiser config derived from these settings."""
        return OptimiserConfig(
            max_driver_overhead_frac=self.max_driver_overhead_frac,
            require_fair_proportion=self.require_fair_proportion,
            objective=self.objective,
            max_candidates_evaluated=self.max_candidates_evaluated,
            corridor_max_offset_km=self.corridor_max_offset_km,
            show_near_miss=self.show_near_miss,
            multi_max_driver_overhead_frac=self.multi_max_driver_overhead_frac,
            multi_route_candidates=self.multi_route_candidates,
            multi_dest_candidates=self.multi_dest_candidates,
            multi_corridor_max_offset_km=self.multi_corridor_max_offset_km,
            multi_num_options=self.multi_num_options,
            multi_verify_top=self.multi_verify_top,
            inter_stop_speed_kmh=self.inter_stop_speed_kmh,
            stop_overhead_min=self.stop_overhead_min,
            pickup_rider_tolerance_min=self.pickup_rider_tolerance_min,
            pickup_pt_leg_max_frac=self.pickup_pt_leg_max_frac,
            pickup_spread_candidates=self.pickup_spread_candidates,
        )

    @property
    def has_onemap_credentials(self) -> bool:
        return bool(
            self.onemap_token_value or (self.onemap_email_value and self.onemap_password_value)
        )


@lru_cache
def get_settings() -> Settings:
    """Return cached settings read from the environment."""
    return Settings()
