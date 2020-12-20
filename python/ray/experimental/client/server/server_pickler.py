"""
Implements the client side of the client/server pickling protocol.

These picklers are aware of the server internals and can find the
references held for the client within the server.

More discussion about the client/server pickling protocol can be found in:

  ray/experimental/client/client_pickler.py

ServerPickler dumps ray objects from the server into the appropriate stubs.
ClientUnpickler loads stubs from the client and finds their associated handle
in the server instance.
"""
import cloudpickle
import io
import sys
import ray

from typing import Any
from typing import TYPE_CHECKING

from ray.experimental.client.client_pickler import PickleStub
from ray.experimental.client.server.server_stubs import (
    ServerSelfReferenceSentinel)

if TYPE_CHECKING:
    from ray.experimental.client.server.server import RayletServicer
    import ray.core.generated.ray_client_pb2 as ray_client_pb2

if sys.version_info < (3, 8):
    try:
        import pickle5 as pickle  # noqa: F401
    except ImportError:
        import pickle  # noqa: F401
else:
    import pickle  # noqa: F401


class ServerPickler(cloudpickle.CloudPickler):
    def __init__(self, client_id: str, server: "RayletServicer", *args,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.client_id = client_id
        self.server = server

    def persistent_id(self, obj):
        if isinstance(obj, ray.ObjectRef):
            obj_id = obj.binary()
            if obj_id not in self.server.object_refs[self.client_id]:
                # We're passing back a reference, probably inside a reference.
                # Let's hold onto it.
                self.server.object_refs[self.client_id][obj_id] = obj
            return PickleStub(
                type="Object",
                client_id=self.client_id,
                ref_id=obj_id,
                name=None,
            )
        elif isinstance(obj, ray.actor.ActorHandle):
            actor_id = obj._actor_id.binary()
            if actor_id not in self.server.actor_refs:
                # We're passing back a handle, probably inside a reference.
                self.actor_refs[actor_id] = obj
            if actor_id not in self.actor_owners[self.client_id]:
                self.actor_owners[self.client_id].add(actor_id)
            return PickleStub(
                type="Actor",
                client_id=self.client_id,
                ref_id=obj._actor_id.binary(),
                name=None,
            )
        return None


class ClientUnpickler(pickle.Unpickler):
    def __init__(self, server, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server = server

    def persistent_load(self, pid):
        assert isinstance(pid, PickleStub)
        if pid.type == "Ray":
            return ray
        elif pid.type == "Object":
            return self.server.object_refs[pid.client_id][pid.ref_id]
        elif pid.type == "Actor":
            return self.server.actor_refs[pid.ref_id]
        elif pid.type == "RemoteFuncSelfReference":
            return ServerSelfReferenceSentinel()
        elif pid.type == "RemoteFunc":
            return self.server.lookup_or_register_func(pid.ref_id,
                                                       pid.client_id)
        elif pid.type == "RemoteActorSelfReference":
            return ServerSelfReferenceSentinel()
        elif pid.type == "RemoteActor":
            return self.server.lookup_or_register_actor(
                pid.ref_id, pid.client_id)
        elif pid.type == "RemoteMethod":
            actor = self.server.actor_refs[pid.ref_id]
            return getattr(actor, pid.name)
        else:
            raise NotImplementedError("Uncovered client data type")


def dumps_from_server(obj: Any,
                      client_id: str,
                      server_instance: "RayletServicer",
                      protocol=None) -> bytes:
    with io.BytesIO() as file:
        sp = ServerPickler(client_id, server_instance, file, protocol=protocol)
        sp.dump(obj)
        return file.getvalue()


def loads_from_client(data: bytes,
                      server_instance: "RayletServicer",
                      *,
                      fix_imports=True,
                      encoding="ASCII",
                      errors="strict") -> Any:
    if isinstance(data, str):
        raise TypeError("Can't load pickle from unicode string")
    file = io.BytesIO(data)
    return ClientUnpickler(
        server_instance, file, fix_imports=fix_imports,
        encoding=encoding).load()


def convert_from_arg(pb: "ray_client_pb2.Arg",
                     server: "RayletServicer") -> Any:
    return loads_from_client(pb.data, server)