from dataclasses import dataclass
from typing import Optional, List

SEPARATOR_TOKEN = "<|endoftext|>"

@dataclass(frozen=True)
class Message:
    user: str
    text: Optional[str] = None

    def render(self):
        if (self.text is None):
            return {}
        global role
        if (self.user == "GPT"):
            role = "assistant"
        else:
            role = "user"
        return {
            "role": role,
            "content": self.text
        }


@dataclass
class Conversation:
    messages: List[Message]

    def prepend(self, message: Message):
        self.messages.insert(0, message)
        return self

    def render(self):
        list = []
        list.append({
          "role": "system",
          "content": "You are a user on discord"
        })
        for message in self.messages:
            list.append(message.render())
        return list


@dataclass(frozen=True)
class Config:
    name: str
    example_conversations: List[Conversation]


@dataclass(frozen=True)
class Prompt:
    convo: Conversation

    def render(self):
        return self.convo.render()
