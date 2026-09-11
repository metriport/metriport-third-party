from http import HTTPStatus
from datetime import datetime
from typing import Any, Literal
from logger import log

from django.db.models import Prefetch
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from canvas_sdk.effects import Effect
from canvas_sdk.effects.simple_api import JSONResponse, Response
from canvas_sdk.handlers.simple_api import BearerCredentials, SimpleAPIRoute
from canvas_sdk.v1.data.note import Note, NoteStates, NoteStateChangeEvent
from canvas_sdk.v1.data.command import Command


METRIPORT_PLUGIN_TOKEN = "METRIPORT_PLUGIN_TOKEN"
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
EXCLUDED_NOTE_TITLE = "Metriport Chart Import"

class NoteAPIProtocol(SimpleAPIRoute):
    """A protocol that returns a patient's notes with their command attributes."""

    PATH = "/routes/notes"

    def authenticate(self, credentials: BearerCredentials) -> bool:
        provided_token = credentials.token
        token = self.secrets[METRIPORT_PLUGIN_TOKEN]
        return provided_token == token

    def get(self) -> list[Response | Effect]:
        try:
            params = NotesQueryParams.from_query_params(self.request.query_params)
        except ValidationError as e:
            return [_bad_request(first_validation_error(e))]
        except ValueError as e:
            return [_bad_request(str(e))]

        try:
            commands_qs = Command.objects.select_related(
                "originator",
                "originator__staff",
                "originator__patient",
                "committer",
                "committer__staff",
                "committer__patient",
                "entered_in_error",
                "entered_in_error__staff",
                "entered_in_error__patient",
            ).order_by("created", "dbid")
            if params.command_type:
                commands_qs = commands_qs.filter(schema_key__in=params.command_type)

            notes_qs = (
                Note.objects.filter(patient__id=params.patient_id)
                .exclude(title=EXCLUDED_NOTE_TITLE)
                .select_related(
                    "patient",
                    "provider",
                    "supervising_provider",
                    "last_modified_by_staff",
                    "note_type_version",
                    "originator",
                    "originator__staff",
                    "originator__patient",
                    "encounter",
                    "location",
                )
                .prefetch_related(
                    Prefetch(
                        "state_history",
                        queryset=NoteStateChangeEvent.objects.select_related(
                            "originator",
                            "originator__staff",
                            "originator__patient",
                        ),
                    ),
                    Prefetch("commands", queryset=commands_qs, to_attr="matching_commands"),
                )
            )
            if params.created_after:
                notes_qs = notes_qs.filter(created__gte=params.created_after)
            if params.created_before:
                notes_qs = notes_qs.filter(created__lte=params.created_before)
            if params.command_type:
                notes_qs = notes_qs.filter(commands__schema_key__in=params.command_type).distinct()

            total = notes_qs.count()
            notes = list(
                notes_qs.order_by("created", "dbid")[params.offset : params.offset + params.limit]
            )

            response = NotesResponse(
                patient_id=params.patient_id,
                notes=[NoteResponse.from_note(note) for note in notes],
                pagination=Pagination(
                    limit=params.limit,
                    offset=params.offset,
                    total=total,
                ),
                filters=params.to_filters(),
            )
            return [
                JSONResponse(
                    response.model_dump(mode="json", exclude_none=True),
                    status_code=HTTPStatus.OK,
                )
            ]

        except Exception as e:
            log.error(f"Error getting notes: {e}")
            return [
                JSONResponse(
                    {"error": str(e)},
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            ]

def _id(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Originator(APIModel):
    id: str
    type: Literal["staff", "patient"] | None = None

    @classmethod
    def from_originator(cls, originator: Any) -> "Originator | None":
        if originator is None:
            return None
        staff = getattr(originator, "staff", None)
        patient = getattr(originator, "patient", None)
        return cls(
            id=_id(getattr(staff, "id", getattr(patient, "id", None))),
            type="staff" if staff is not None else ("patient" if patient is not None else None),
        )


class NoteType(APIModel):
    id: str | None = None
    name: str | None = None
    display: str | None = None
    code: str | None = None
    system: str | None = None
    version: str | None = None
    category: str | None = None
    is_telehealth: bool | None = None
    is_billable: bool | None = None
    is_scheduleable: bool | None = None
    is_patient_required: bool | None = None
    default_place_of_service: str | None = None
    unique_identifier: str | None = None

    @classmethod
    def from_note_type(cls, note_type: Any) -> "NoteType | None":
        if note_type is None:
            return None
        return cls(
            id=_id(getattr(note_type, "id", None)),
            name=getattr(note_type, "name", None),
            display=getattr(note_type, "display", None),
            code=getattr(note_type, "code", None),
            system=getattr(note_type, "system", None),
            version=_str(getattr(note_type, "version", None)),
            category=_str(getattr(note_type, "category", None)),
            is_telehealth=getattr(note_type, "is_telehealth", None),
            is_billable=getattr(note_type, "is_billable", None),
            is_scheduleable=getattr(note_type, "is_scheduleable", None),
            is_patient_required=getattr(note_type, "is_patient_required", None),
            default_place_of_service=_str(getattr(note_type, "default_place_of_service", None)),
            unique_identifier=_id(getattr(note_type, "unique_identifier", None)),
        )


class NoteStateEvent(APIModel):
    id: str | None = None
    state: NoteStates
    created: datetime | None = None
    originator: Originator | None = None

    @classmethod
    def from_event(cls, event: NoteStateChangeEvent) -> "NoteStateEvent":
        return cls(
            id=_id(getattr(event, "id", None)),
            state=NoteStates(event.state),
            created=getattr(event, "created", None),
            originator=Originator.from_originator(getattr(event, "originator", None)),
        )


class NoteCommand(APIModel):
    id: str
    dbid: int | None = None
    schema_key: str | None = None
    state: str | None = None
    data: Any = None
    created: datetime | None = None
    modified: datetime | None = None
    origination_source: str | None = None
    custom_html: str | None = None
    originator: Originator | None = None
    committer: Originator | None = None
    entered_in_error: Originator | None = None

    @classmethod
    def from_command(cls, command: Command) -> "NoteCommand":
        return cls(
            id=str(command.id),
            dbid=getattr(command, "dbid", None),
            schema_key=getattr(command, "schema_key", None),
            state=getattr(command, "state", None),
            data=getattr(command, "data", None),
            created=getattr(command, "created", None),
            modified=getattr(command, "modified", None),
            origination_source=getattr(command, "origination_source", None),
            custom_html=_str(getattr(command, "custom_html", None)),
            originator=Originator.from_originator(getattr(command, "originator", None)),
            committer=Originator.from_originator(getattr(command, "committer", None)),
            entered_in_error=Originator.from_originator(getattr(command, "entered_in_error", None)),
        )


class NoteResponse(APIModel):
    id: str
    dbid: int | None = None
    title: str | None = None
    created: datetime | None = None
    modified: datetime | None = None
    datetime_of_service: datetime | None = None
    place_of_service: str | None = None
    patient_id: str | None = None
    provider_id: str | None = None
    supervising_provider_id: str | None = None
    last_modified_by_staff_id: str | None = None
    location_id: str | None = None
    encounter_id: str | None = None
    originator: Originator | None = None
    note_type: NoteType | None = None
    related_data: Any = None
    state_history: list[NoteStateEvent] = Field(default_factory=list)
    commands: list[NoteCommand] = Field(default_factory=list)

    @classmethod
    def from_note(cls, note: Note) -> "NoteResponse":
        patient = getattr(note, "patient", None)
        return cls(
            id=str(note.id),
            dbid=getattr(note, "dbid", None),
            title=getattr(note, "title", None),
            created=getattr(note, "created", None),
            modified=getattr(note, "modified", None),
            datetime_of_service=getattr(note, "datetime_of_service", None),
            place_of_service=_str(getattr(note, "place_of_service", None)),
            patient_id=_id(getattr(patient, "id", None)),
            provider_id=_id(getattr(getattr(note, "provider", None), "id", None)),
            supervising_provider_id=_id(
                getattr(getattr(note, "supervising_provider", None), "id", None)
            ),
            last_modified_by_staff_id=_id(
                getattr(getattr(note, "last_modified_by_staff", None), "id", None)
            ),
            location_id=_id(getattr(getattr(note, "location", None), "id", None)),
            encounter_id=_id(getattr(getattr(note, "encounter", None), "id", None)),
            originator=Originator.from_originator(getattr(note, "originator", None)),
            note_type=NoteType.from_note_type(getattr(note, "note_type_version", None)),
            related_data=getattr(note, "related_data", None) or None,
            state_history=[
                NoteStateEvent.from_event(event) for event in note.state_history.all()
            ],
            commands=[
                NoteCommand.from_command(command)
                for command in getattr(note, "matching_commands", [])
            ],
        )


class Pagination(APIModel):
    limit: int
    offset: int
    total: int


class NotesFilters(APIModel):
    command_type: list[str] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


class NotesResponse(APIModel):
    patient_id: str
    notes: list[NoteResponse]
    pagination: Pagination
    filters: NotesFilters | None = None


class NotesQueryParams(BaseModel):
    patient_id: str
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0)
    command_type: list[str] = Field(default_factory=list)
    created_after: datetime | None = None
    created_before: datetime | None = None

    @classmethod
    def from_query_params(cls, query_params: Any) -> "NotesQueryParams":
        data = _query_params_as_dict(query_params)
        data["command_type"] = _split_command_types(
            data.pop("command_type", None) or data.pop("schema_key", None)
        )
        params = cls.model_validate(data)
        if (
            params.created_after
            and params.created_before
            and params.created_after > params.created_before
        ):
            raise ValueError("created_after must be before created_before")
        return params

    def to_filters(self) -> NotesFilters | None:
        if not self.command_type and not self.created_after and not self.created_before:
            return None
        return NotesFilters(
            command_type=self.command_type or None,
            created_after=self.created_after,
            created_before=self.created_before,
        )


def _split_command_types(value: Any) -> list[str]:
    """Accept a repeated or comma-separated query param as a list of command types."""
    if value is None or value == "":
        return []
    values = value if isinstance(value, list) else [value]
    return [
        part.strip() for item in values for part in str(item).split(",") if part.strip()
    ]


def _query_params_as_dict(query_params: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    items = (
        query_params.multi_items()
        if hasattr(query_params, "multi_items")
        else query_params.items()
    )
    for key, value in items:
        if value in (None, ""):
            continue
        if key in data:
            existing = data[key]
            data[key] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            data[key] = value
    return data


def first_validation_error(error: ValidationError) -> str:
    """Render the first pydantic error as `field: message`."""
    first = error.errors()[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    return f"{loc}: {first['msg']}" if loc else first["msg"]


def _bad_request(message: str) -> Response:
    return JSONResponse({"error": message}, status_code=HTTPStatus.BAD_REQUEST)

