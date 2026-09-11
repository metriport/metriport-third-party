from datetime import datetime
from http import HTTPStatus
from typing import Any, Literal, TypedDict

from canvas_sdk.effects import Effect
from canvas_sdk.effects.simple_api import JSONResponse, Response
from canvas_sdk.handlers.simple_api import (
    BearerCredentials,
    SimpleAPIRoute,
)
from canvas_sdk.v1.data.command import Command
from canvas_sdk.v1.data.note import Note, NoteStateChangeEvent, NoteStates
from django.db.models import Prefetch
from logger import log
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

METRIPORT_PLUGIN_TOKEN = "METRIPORT_PLUGIN_TOKEN"
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
EXCLUDED_NOTE_TITLE = "Metriport Chart Import"

class NoteAPIProtocol(SimpleAPIRoute):
    """A protocol that returns a patient's notes with the note's associated command(s)."""

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
                        to_attr="matching_state_events",
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


def _related_id(related: Any) -> str | None:
    if related is None:
        return None
    return str(related.id)


class NoteBodyLine(TypedDict):
    type: Literal["text", "command"]
    value: Any


def _note_texts(body: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for raw in body:
        line_type = raw.get("type")
        if line_type not in ("text", "command"):
            continue
        line: NoteBodyLine = {"type": line_type, "value": raw.get("value")}
        value = line["value"]
        if line["type"] == "text" and isinstance(value, str):
            texts.append(value)

    # Canvas adds several blank lines by default, strip out pre/post blank lines
    start = 0
    end = len(texts)
    while start < end and texts[start] == "":
        start += 1
    while end > start and texts[end - 1] == "":
        end -= 1
    return texts[start:end]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Originator(APIModel):
    id: str
    type: Literal["staff"]

    @classmethod
    def from_originator(cls, originator: Any) -> "Originator | None":
        if originator is None:
            return None
        # Restrict to staff-only
        staff_id = _related_id(getattr(originator, "staff", None))
        if staff_id is not None:
            return cls(id=staff_id, type="staff")
        return None


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
            id=_id(note_type.id),
            name=note_type.name,
            display=note_type.display,
            code=note_type.code,
            system=note_type.system,
            version=_str(note_type.version),
            category=_str(note_type.category),
            is_telehealth=note_type.is_telehealth,
            is_billable=note_type.is_billable,
            is_scheduleable=note_type.is_scheduleable,
            is_patient_required=note_type.is_patient_required,
            default_place_of_service=_str(note_type.default_place_of_service),
            unique_identifier=_id(note_type.unique_identifier),
        )


class NoteStateEvent(APIModel):
    id: str | None = None
    state: NoteStates
    created: datetime | None = None
    originator: Originator | None = None

    @classmethod
    def from_event(cls, event: NoteStateChangeEvent) -> "NoteStateEvent":
        return cls(
            id=_id(event.id),
            state=NoteStates(event.state),
            created=event.created,
            originator=Originator.from_originator(event.originator),
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
            dbid=command.dbid,
            schema_key=command.schema_key,
            state=command.state,
            data=command.data,
            created=command.created,
            modified=command.modified,
            origination_source=command.origination_source,
            custom_html=_str(command.custom_html),
            originator=Originator.from_originator(command.originator),
            committer=Originator.from_originator(command.committer),
            entered_in_error=Originator.from_originator(command.entered_in_error),
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
    texts: list[str] = Field(default_factory=list)

    @classmethod
    def from_note(cls, note: Note) -> "NoteResponse":
        return cls(
            id=str(note.id),
            dbid=note.dbid,
            title=note.title,
            created=note.created,
            modified=note.modified,
            datetime_of_service=note.datetime_of_service,
            place_of_service=_str(note.place_of_service),
            patient_id=_related_id(note.patient),
            provider_id=_related_id(note.provider),
            supervising_provider_id=_related_id(note.supervising_provider),
            last_modified_by_staff_id=_related_id(note.last_modified_by_staff),
            location_id=_related_id(note.location),
            encounter_id=_related_id(getattr(note, "encounter", None)),
            originator=Originator.from_originator(note.originator),
            note_type=NoteType.from_note_type(note.note_type_version),
            related_data=note.related_data or None,
            state_history=[
                NoteStateEvent.from_event(event)
                for event in getattr(note, "matching_state_events", [])
            ],
            commands=[
                NoteCommand.from_command(command)
                for command in getattr(note, "matching_commands", [])
            ],
            texts=_note_texts(note.body),
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
