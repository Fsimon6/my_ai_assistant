from typing import TypedDict, Any, Literal


class SearchResult(TypedDict):
    content: str
    metadata: dict[str, Any]
    score: float
    id: str


class ChatMessage(TypedDict):
    role: Literal['system', 'user', 'assistant']
    content: str
