"""Backend for icatdump and icatingest.

This module provides the base classes DumpFileReader and
DumpFileWriter that define the API and the logic for reading and
writing ICAT data files.  The actual work is done in file format
specific modules that should provide subclasses that must implement
the abstract methods.

Data files are partitioned in chunks.  This is done to avoid having
the whole file, e.g. the complete inventory of the ICAT, at once in
memory.  The problem is that objects contain references to other
objects (e.g. Datafiles refer to Datasets, the latter refer to
Investigations, and so forth).  We keep an index of the objects in
order to resolve these references.  But there is a memory versus time
tradeoff: we cannot keep all the objects in the index, that would
again mean the complete inventory of the ICAT.  And we can't know
beforehand which object is going to be referenced later on, so we
don't know which one to keep and which one to discard from the index.
Fortunately we can query objects we discarded once back from the ICAT
server with :meth:`icat.client.Client.searchUniqueKey`.  But this is
expensive.  So the strategy is as follows: keep all objects from the
current chunk in the index and discard the complete index each time a
chunk has been processed.  This will work fine if objects are mostly
referencing other objects from the same chunk and only a few
references go across chunk boundaries.

Therefore, we want these chunks to be small enough to fit into memory,
but at the same time large enough to keep as many relations between
objects as possible local in a chunk.  It is in the responsibility of
the writer of the data file to create the chunks in this manner.

The objects that get written to the data file and how this file is
organized is controlled by lists of ICAT search expressions, see
:meth:`icat.dumpfile.DumpFileWriter.writeobjs`.  There is some degree
of flexibility: an object may include related objects in an
one-to-many relation, just by including them in the search expression.
In this case, these related objects should not have a search
expression on their own again.  For instance, the search expression
for Grouping may include UserGroup.  The UserGroups will then be
embedded in their respective grouping in the data file.  There should
not be a search expression for UserGroup then.

Objects related in a many-to-one relation must always be included in
the search expression.  This is also true if the object is
indirectly related to one of the included objects.  In this case,
only a reference to the related object will be included in the data
file.  The related object must have its own list entry.
"""

import sys
import icat
from icat.query import Query


# ------------------------------------------------------------
# DumpFileReader
# ------------------------------------------------------------

class DumpFileReader(object):
    """Base class for backends that read a data file."""

    def __init__(self, client, infile):
        self.client = client
        self.infile = infile

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.infile.close()

    def getdata(self):
        """Iterate over the chunks in the data file.

        Yield some data object in each iteration.  This data object is
        specific to the implementing backend and should be passed as
        the `data` argument to
        :meth:`icat.dumpfile.DumpFileReader.getobjs_from_data`.
        """
        raise NotImplementedError

    def getobjs_from_data(self, data, objindex):
        """Iterate over the objects in a data chunk.

        Yield a new entity object in each iteration.  The object is
        initialized from the data, but not yet created at the client.
        """
        raise NotImplementedError

    def getobjs(self, objindex=None):
        """Iterate over the objects in the data file.

        Yield a new entity object in each iteration.  The object is
        initialized from the data, but not yet created at the client.

        :param objindex: cache of previously retrieved objects, used
            to resolve object relations.  See the
            :meth:`icat.client.Client.searchUniqueKey` for details.
            If this is :const:`None`, an internal cache will be used
            that is purged at the start of every new data chunk.
        :type objindex: :class:`dict`
        """
        resetindex = (objindex is None)
        for data in self.getdata():
            if resetindex:
                objindex = {}
            for key, obj in self.getobjs_from_data(data, objindex):
                yield obj
                obj.truncateRelations()
                if key:
                    objindex[key] = obj


# ------------------------------------------------------------
# DumpFileWriter
# ------------------------------------------------------------

class DumpFileWriter(object):
    """Base class for backends that write a data file."""

    def __init__(self, client, outfile):
        self.client = client
        self.outfile = outfile
        self.idcounter = {}

    def __enter__(self):
        self.head()
        return self

    def __exit__(self, type, value, traceback):
        if type is None:
            self.finalize()
        self.outfile.close()

    def head(self):
        """Write a header with some meta information to the data file."""
        raise NotImplementedError

    def startdata(self):
        """Start a new data chunk.

        If the current chunk contains any data, write it to the data
        file.
        """
        raise NotImplementedError

    def writeobj(self, key, obj, keyindex):
        """Add an entity object to the current data chunk."""
        raise NotImplementedError

    def finalize(self):
        """Finalize the data file."""
        raise NotImplementedError

    def writeobjs(self, objs, keyindex, chunksize=100):
        """Write some entity objects to the current data chunk.

        The objects are searched from the ICAT server.  The key index
        is used to serialize object relations in the data file.  For
        object types that do not have an appropriate uniqueness
        constraint in the ICAT schema, a generic key is generated.
        These objects may only be referenced from the same chunk in
        the data file.

        :param objs: query to search the objects, either a Query
            object or a string.  It must contain appropriate INCLUDE
            statements to include all related objects from many-to-one
            relations.  These related objects must also include all
            informations needed to generate their unique key, unless
            they are registered in the key index already.

            Furthermore, related objects from one-to-many relations
            may be included.  These objects will then be embedded with
            the relating object in the data file.  The same
            requirements for including their respective related
            objects apply.

            As an alternative to a query, objs may also be a list of
            entity objects.  The same conditions on the inclusion of
            related objects apply.
        :type objs: :class:`icat.query.Query` or :class:`str` or
            :class:`list`
        :param keyindex: cache of generated keys.  It maps object ids
            to unique keys.  See the
            :meth:`icat.entity.Entity.getUniqueKey` for details.
        :type keyindex: :class:`dict`
        :param chunksize: tuning parameter, see
            :meth:`icat.client.Client.searchChunked` for details.
        :type chunksize: :class:`int`
        """
        if isinstance(objs, Query) or isinstance(objs, basestring):
            objs = self.client.searchChunked(objs, chunksize=chunksize)
        else:
            objs.sort(key=icat.entity.Entity.__sortkey__)
        for obj in objs:
            # Entities without a constraint will use their id to form
            # the unique key as a last resort.  But we want the keys
            # not to depend on volatile attributes such as the id.
            # Use a generic numbered key for the concerned entity
            # types instead.
            if 'id' in obj.Constraint:
                t = obj.BeanName
                if t not in self.idcounter:
                    self.idcounter[t] = 0
                self.idcounter[t] += 1
                k = "%s_%08d" % (t, self.idcounter[t])
                keyindex[(obj.BeanName, obj.id)] = k
            else:
                k = obj.getUniqueKey(keyindex=keyindex)
            self.writeobj(k, obj, keyindex)

    def writedata(self, objs, keyindex=None, chunksize=100):
        """Write a data chunk.

        :param objs: an iterable that yields either queries to search
            for the objects or object lists.  See
            :meth:`icat.dumpfile.DumpFileWriter.writeobjs` for
            details.
        :param keyindex: cache of generated keys, see
            :meth:`icat.dumpfile.DumpFileWriter.writeobjs` for
            details.  If this is :const:`None`, an internal index will
            be used.
        :type keyindex: :class:`dict`
        :param chunksize: tuning parameter, see
            :meth:`icat.client.Client.searchChunked` for details.
        :type chunksize: :class:`int`
        """
        if keyindex is None:
            keyindex = {}
        self.startdata()
        for o in objs:
            self.writeobjs(o, keyindex, chunksize=chunksize)


# ------------------------------------------------------------
# Register of backends and open_dumpfile()
# ------------------------------------------------------------

Backends = {}
"""A register of all known backends."""

def register_backend(formatname, reader, writer):
    """Register a backend.

    This function should be called by file format specific backends at
    initialization.

    :param formatname: name of the file format that the backend
        implements.
    :type formatname: :class:`str`
    :param reader: class for reading data files.  Should be a subclass
        of :class:`icat.dumpfile.DumpFileReader`.
    :param writer: class for writing data files.  Should be a subclass
        of :class:`icat.dumpfile.DumpFileWriter`.
    """
    Backends[formatname] = (reader, writer)

def open_dumpfile(client, f, formatname, mode):
    """Open a data file, either for reading or for writing.

    Note that (subclasses of) :class:`icat.dumpfile.DumpFileReader`
    and :class:`icat.dumpfile.DumpFileWriter` may be used as context
    managers.  This function is suitable to be used in the
    :obj:`with` statement.

    >>> with open_dumpfile(client, f, "XML", 'r') as dumpfile:
    ...     for obj in dumpfile.getobjs():
    ...         obj.create()

    :param client: the ICAT client.
    :type client: :class:`icat.client.Client`
    :param f: a file object or the name of file.  In the former case,
        the file must be opened in the appropriate mode, in the latter
        case a file by that name is opened using `mode`.  The special
        value of "-" may be used as an alias for :data:`sys.stdin` or
        :data:`sys.stdout`.
    :param formatname: name of the file format that has been registered by
        the backend.
    :type formatname: :class:`str`
    :param mode: a string indicating how the file is to be opened.
        The first character must be either "r" or "w" for reading or
        writing respectively.
    :type mode: :class:`str`
    :return: an instance of the appropriate class.  This is either the
        reader or the writer class, according to the mode, that has
        been registered by the backend.
    :raise ValueError: if the format is not known or if the mode does
        not start with "r" or "w".
    """
    if formatname not in Backends:
        raise ValueError("Unknown data file format '%s'" % formatname)
    if mode[0] == 'r':
        if isinstance(f, basestring):
            if f == "-":
                f = sys.stdin
            else:
                f = open(f, mode)
        cls = Backends[formatname][0]
        return cls(client, f)
    elif mode[0] == 'w':
        if isinstance(f, basestring):
            if f == "-":
                f = sys.stdout
            else:
                f = open(f, mode)
        cls = Backends[formatname][1]
        return cls(client, f)
    else:
        raise ValueError("Invalid file mode '%s'" % mode)

