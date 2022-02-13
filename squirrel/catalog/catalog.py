from __future__ import annotations

import io
import json
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    KeysView,
    List,
    MutableMapping,
    NamedTuple,
    Optional,
    Tuple,
    Type,
    Union,
)

from squirrel.catalog.source import Source
from squirrel.fsspec.fs import get_fs_from_url

if TYPE_CHECKING:
    from ruamel.yaml import Constructor, Representer, SequenceNode

    from squirrel.driver import Driver

__all__ = ["Catalog", "CatalogKey"]


class CatalogKey(NamedTuple):
    # Defines a key in a catalog consisting of the identifier and the version of a source
    identifier: str
    version: int = -1

    @classmethod
    def to_yaml(cls, representer: Representer, obj: CatalogKey) -> SequenceNode:
        """Serializes object to SequenceNode."""
        return representer.represent_sequence("!CatalogKey", obj)

    @classmethod
    def from_yaml(cls, constructor: Constructor, node: SequenceNode) -> CatalogKey:
        """Deserializes object from SequenceNode."""
        return CatalogKey(*constructor.construct_sequence(node))


class Catalog(MutableMapping):
    def __init__(self) -> None:
        """Init a Catalog object"""
        self._sources = dict()

    def __repr__(self) -> str:  # noqa D105
        return str(set(self.sources.keys()))

    def __eq__(self, other: Any) -> bool:  # noqa D105
        if not isinstance(other, Catalog):
            return False

        if len(self.difference(other)) > 0:
            return False

        # deep equal
        for k in self.keys():
            for v in self[k].versions.keys():
                if self[k][v] != other[k][v]:
                    return False

        return True

    def __contains__(self, identifier: str) -> bool:  # noqa D105
        return identifier in self._sources.keys()

    def __delitem__(self, identifier: Union[str, CatalogKey]) -> None:  # noqa D105
        del self._sources[identifier]

    def __setitem__(self, identifier: str, value: Source) -> None:  # noqa D105
        self._sources[identifier] = CatalogSource(source=value, identifier=identifier, catalog=self)

    def __getitem__(self, identifier: str) -> CatalogSource:  # noqa D105
        if identifier not in self:
            # return a dummy object to let the user set a version directly
            return DummyCatalogSource(identifier, self)
        return self.sources[identifier][-1]

    def items(self) -> Tuple[str, Source]:  # noqa D105
        return self.__iter__()

    def __iter__(self) -> Iterator[Tuple[str, Source]]:  # noqa D105
        for k, v in self.sources.items():
            yield k, v[-1]

    def keys(self) -> KeysView[str]:  # noqa D105
        return self.sources.keys()

    def __len__(self) -> int:  # noqa D105
        return len(self.keys())

    def copy(self) -> Catalog:
        """Return a deep copy of catalog"""
        # To be 100% save, serialize to string and back
        from squirrel.catalog.yaml import catalog2yamlcatalog, prep_yaml, yamlcatalog2catalog

        yaml = prep_yaml()
        ret = None

        with io.StringIO() as fh:
            yaml.dump(catalog2yamlcatalog(self), fh)
            fh.seek(0)
            ret = yamlcatalog2catalog(yaml.load(fh.read()))
        return ret

    def slice(self, keys: List[str]) -> Catalog:
        """Return a deep copy of catalog were only by key specified sources get copied."""
        cat_cp = self.copy()
        cat = Catalog()
        for k in keys:
            for v in cat_cp[k].versions:
                cat[k][v] = cat_cp[k][v]
        return cat

    def join(self, other: Catalog) -> Catalog:
        """Return a joined Catalog out of two disjoint Catalogs."""
        assert len(self.intersection(other)) == 0
        return self.union(other)

    def difference(self, other: Catalog) -> Catalog:
        """Return a Catalog which consists of the difference of the input Catalogs."""
        cat1 = self.copy()
        cat2 = other.copy()

        new_cat = Catalog()
        for a_cat1, a_cat_2 in [(cat1, cat2), (cat2, cat1)]:
            for k in a_cat1.keys():
                for ver_id, version in a_cat1[k].versions.items():
                    if k not in a_cat_2 or ver_id not in a_cat_2[k]:
                        new_cat[k][ver_id] = version
        return new_cat

    def union(self, other: Catalog) -> Catalog:
        """Return a Catalog which consists of the union of the input Catalogs."""
        cat = self.copy()
        oth_cp = other.copy()

        for k in oth_cp.keys():
            for ver_id, version in oth_cp[k].versions.items():
                cat[k][ver_id] = version
        return cat

    def intersection(self, other: Catalog) -> Catalog:
        """Return a Catalog which consists of the intersection of the input Catalogs."""
        cat1 = self.copy()
        cat2 = other.copy()

        new_cat = Catalog()
        for k in cat1.keys():
            for ver_id, version in cat1[k].versions.items():
                if k in cat2 and ver_id in cat2[k].versions:
                    assert cat1[k][ver_id] == cat2[k][ver_id]
                    new_cat[k][ver_id] = version
        return new_cat

    def filter(self: Catalog, predicate: Callable[[CatalogSource], bool]) -> Catalog:
        """Filter catalog sources based on a predicate."""
        cat1 = self.copy()

        new_cat = Catalog()
        for k in cat1.keys():
            for ver_id, version in cat1[k].versions.items():
                if predicate(version):
                    new_cat[k][ver_id] = version
        return new_cat

    @staticmethod
    def from_plugins() -> Catalog:
        """Returns a Catalog containing sources specified by plugins."""
        from squirrel.framework.plugins.plugin_manager import squirrel_plugin_manager

        ret = Catalog()
        plugins = squirrel_plugin_manager.hook.squirrel_sources()
        for plugin in plugins:
            for s_key, source in plugin:
                ret[s_key.identifier][s_key.version] = source

        return ret

    @staticmethod
    def from_dirs(paths: List[str]) -> Catalog:
        """Create a Catalog based on a list of folders containing yaml files."""
        files = []
        for path in paths:
            fs = get_fs_from_url(path)
            a_files = [f for f in fs.ls(path) if f.endswith(".yaml")]
            files += a_files
        return Catalog.from_files(files)

    @staticmethod
    def from_files(paths: List[str]) -> Catalog:
        """Create a Catalog based on a list of paths to yaml files."""
        cat = Catalog()
        for file in paths:
            fs = get_fs_from_url(file)
            with fs.open(file) as fh:
                new_cat = Catalog.from_str(fh.read())
                cat = cat.join(new_cat)
        return cat

    @staticmethod
    def from_str(cat: str) -> Catalog:
        """Create a Catalog based on a yaml string."""
        from squirrel.catalog.yaml import prep_yaml, yamlcatalog2catalog

        yaml = prep_yaml()
        return yamlcatalog2catalog(yaml.load(cat))

    def to_file(self, path: str) -> None:
        """Save a Catalog to a yaml file at the specified path."""
        from squirrel.catalog.yaml import catalog2yamlcatalog, prep_yaml

        yaml = prep_yaml()
        fs = get_fs_from_url(path)
        with fs.open(path, mode="w+") as fh:
            ser = catalog2yamlcatalog(self)
            yaml.dump(ser, fh)

    @property
    def sources(self) -> Dict[str, CatalogSource]:
        """Read only property"""
        return self._sources


class CatalogSource(Source):
    """Represents a specific version of a source in catalog."""

    def __init__(
        self,
        source: Source,
        identifier: str,
        catalog: Catalog,
        version: int = 1,
    ) -> None:
        """Initialize CatalogSource using a Source."""
        super().__init__(driver_name=source.driver_name, driver_kwargs=source.driver_kwargs, metadata=source.metadata)
        self._identifier = identifier
        self._version = version
        self._catalog = catalog

    def __eq__(self, other: Any) -> bool:  # noqa D105
        if not isinstance(other, CatalogSource):
            return False
        if self.identifier != other.identifier:
            return False
        if self.version != other.version:
            return False
        return super().__eq__(other)

    def __repr__(self) -> str:  # noqa D105
        vars = ("identifier", "driver_name", "driver_kwargs", "metadata", "version")
        dct = {k: getattr(self, k) for k in vars}
        return json.dumps(dct, indent=2, default=str)

    @property
    def identifier(self) -> str:
        """Identifier of the source, read-only."""
        return self._identifier

    @property
    def version(self) -> int:
        """Version of the source, read-only."""
        return self._version

    @property
    def catalog(self) -> Catalog:
        """Catalog containing the source, read-only."""
        return self._catalog

    def get_driver(self, **kwargs) -> Driver:
        """Returns an instance of the driver specified by the source."""
        from squirrel.framework.plugins.plugin_manager import squirrel_plugin_manager

        plugins: List[List[Type[Driver]]] = squirrel_plugin_manager.hook.squirrel_drivers()
        for plugin in plugins:
            for driver_cls in plugin:
                if driver_cls.name == self.driver_name:
                    return driver_cls(catalog=self.catalog, **{**self.driver_kwargs, **kwargs})

        raise ValueError(f"Driver {self.driver_name} not found.")


class DummyCatalogSource:
    def __init__(
        self,
        identifier: str,
        catalog: Catalog,
    ) -> None:
        """Init a dummy catalog source to assign versions even if source does not exist yet"""
        self._identifier = identifier
        self._catalog = catalog

    def __setitem__(self, index: int, value: Source) -> None:  # noqa D105
        assert index > 0
        self._catalog._sources[self.identifier] = CatalogSource(
            source=value, identifier=self.identifier, catalog=self._catalog, version=index
        )

    def __contains__(self, index: str) -> bool:  # noqa D105
        return False

    @property
    def identifier(self) -> str:
        """Read only property"""
        return self._identifier
