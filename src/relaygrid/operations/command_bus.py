from __future__ import annotations
from dataclasses import dataclass,field
from collections import deque

@dataclass(frozen=True)
class Command:
    command_id:str
    device_id:str
    action:str
    issued_at:float
    expires_at:float
    actor:str

@dataclass(frozen=True)
class CommandResult:
    command_id:str
    status:str
    completed_at:float
    observed_state:str|None=None

@dataclass
class CommandBus:
    pending:deque[Command]=field(default_factory=deque)
    completed:dict[str,CommandResult]=field(default_factory=dict)
    seen_ids:set[str]=field(default_factory=set)
    held:set[str]=field(default_factory=set)

    def submit(self,command:Command,now:float):
        if command.command_id in self.seen_ids: raise ValueError("duplicate command")
        if command.expires_at<=now: raise ValueError("expired command")
        if command.action not in {"open","close","trip","reset"}: raise ValueError("unsupported action")
        self.seen_ids.add(command.command_id); self.pending.append(command)

    def next_for(self,device_id:str,now:float):
        for command in list(self.pending):
            if command.expires_at<=now:
                self.pending.remove(command); self.completed[command.command_id]=CommandResult(command.command_id,"expired",now,None)
                continue
            if command.device_id==device_id:
                if command.command_id in self.held: raise PermissionError("command held pending independent reconfirmation")
                self.pending.remove(command); return command
        return None

    def hold(self,command_id:str):
        if not any(c.command_id==command_id for c in self.pending): raise KeyError(command_id)
        self.held.add(command_id)

    def release_hold(self,command_id:str): self.held.discard(command_id)

    def is_held(self,command_id:str): return command_id in self.held

    def complete(self,command:Command,status:str,completed_at:float,observed_state:str|None=None):
        if status not in {"success","failed","rejected"}: raise ValueError(status)
        self.completed[command.command_id]=CommandResult(command.command_id,status,completed_at,observed_state)

    def outstanding_for(self,device_id:str): return [c for c in self.pending if c.device_id==device_id]
