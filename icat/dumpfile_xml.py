"""XML data file backend for icatdump.py and icatingest.py.
"""

import os
import datetime
from lxml import etree
import icat
import icat.dumpfile
from icat.query import Query
try:
    utc = datetime.timezone.utc
except AttributeError:
    try:
        from suds.sax.date import UtcTimezone
        utc = UtcTimezone()
    except ImportError:
        utc = None


# ------------------------------------------------------------
# Helper
# ------------------------------------------------------------

def _entity2elem(obj, tag, keyindex):
    """Convert an entity object to an etree.Element."""
    if tag is None:
        tag = obj.instancetype
    d = etree.Element(tag)

    for attr in sorted(obj.InstAttr):
        if attr == 'id':
            continue
        v = getattr(obj, attr, None)
        if v is None:
            continue
        elif isinstance(v, bool):
            v = str(v).lower()
        elif isinstance(v, (int, long)):
            v = str(v)
        elif isinstance(v, datetime.datetime):
            if v.tzinfo is not None and v.tzinfo.utcoffset(v) is not None:
                # v has timezone info.  This will be the timezone set
                # in the ICAT server.  Convert it to UTC to avoid
                # dependency of server settings in the dumpfile.
                # Assume v.isoformat() to have a valid timezone
                # suffix.
                if utc:
                    v = v.astimezone(utc)
                v = v.isoformat()
            else:
                # v has no timezone info, assume it to be UTC, append
                # the corresponding timezone suffix.
                v = v.isoformat() + 'Z'
        else:
            try:
                v = str(v)
            except UnicodeError:
                v = unicode(v)
        etree.SubElement(d, attr).text = v

    for attr in sorted(obj.InstRel):
        o = getattr(obj, attr, None)
        if o is not None:
            k = o.getUniqueKey(keyindex=keyindex)
            etree.SubElement(d, attr, ref=k)

    for attr in sorted(obj.InstMRel):
        for o in sorted(getattr(obj, attr), 
                        key=icat.entity.Entity.__sortkey__):
            d.append(_entity2elem(o, tag=attr, keyindex=keyindex))

    return d

def _searchByReference(client, element, objtype, objindex):
    """Search for a referenced object.
    """
    ref = element.get('ref')
    if ref:
        # object is referenced by key.
        return client.searchUniqueKey(ref, objindex)
    else:
        # object is referenced by attributes.
        attrs = set(element.keys()) - {'id'}
        conditions = { a: "= '%s'" % element.get(a) for a in attrs }
        query = Query(client, objtype, conditions=conditions)
        return client.assertedSearch(query)[0]

def _elem2entity(client, insttypemap, element, objtype, objindex):
    """Create an entity object from XML element data."""
    obj = client.new(insttypemap[objtype])
    for subelem in element:
        attr = subelem.tag
        if attr in obj.AttrAlias:
            attr = obj.AttrAlias[attr]
        if attr in obj.InstAttr:
            setattr(obj, attr, subelem.text)
        elif attr in obj.InstRel:
            rtype = obj.getAttrType(attr)
            robj = _searchByReference(client, subelem, rtype, objindex)
            setattr(obj, attr, robj)
        elif attr in obj.InstMRel:
            rtype = obj.getAttrType(attr)
            robj = _elem2entity(client, insttypemap, subelem, rtype, objindex)
            getattr(obj, attr).append(robj)
        else:
            raise ValueError("invalid subelement '%s' in '%s'" 
                             % (subelem.tag, element.tag))
    return obj


# ------------------------------------------------------------
# XMLDumpFileReader
# ------------------------------------------------------------

class XMLDumpFileReader(icat.dumpfile.DumpFileReader):
    """Backend for icatingest.py to read a XML data file."""

    def __init__(self, client, infile):
        super(XMLDumpFileReader, self).__init__(client, infile)
        # need binary mode for infile
        self.infile = os.fdopen(os.dup(infile.fileno()), 'rb')
        self.insttypemap = { c.BeanName:t 
                             for t,c in self.client.typemap.iteritems() }

    def getdata(self):
        """Iterate over the chunks in the data file.
        """
        for event, data in etree.iterparse(self.infile, tag='data'):
            yield data
            data.clear()

    def getobjs_from_data(self, data, objindex):
        """Iterate over the objects in a data chunk.

        Yield a new entity object in each iteration.  The object is
        initialized from the data, but not yet created at the client.
        """
        for elem in data:
            key = elem.get('id')
            tag = elem.tag
            if tag.endswith("Ref"):
                # elem is a reference to an already existing object.
                # Do not yield it as it should not be created, but add
                # it to the objindex so it can be referenced later on
                # from other objects.
                if key:
                    objtype = self.client.typemap[tag[0:-3]].BeanName
                    obj = _searchByReference(self.client, elem, objtype, 
                                             objindex)
                    objindex[key] = obj
            else:
                objtype = self.client.typemap[tag].BeanName
                obj = _elem2entity(self.client, self.insttypemap, 
                                   elem, objtype, objindex)
                yield key, obj


# ------------------------------------------------------------
# XMLDumpFileWriter
# ------------------------------------------------------------

class XMLDumpFileWriter(icat.dumpfile.DumpFileWriter):
    """Backend for icatdump.py to write a XML data file."""

    def __init__(self, client, outfile):
        super(XMLDumpFileWriter, self).__init__(client, outfile)
        # need binary mode for outfile
        self.outfile = os.fdopen(os.dup(outfile.fileno()), 'wb')
        self.data = etree.Element("data")

    def head(self):
        """Write a header with some meta information to the data file."""
        date = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
        head = etree.Element("head")
        etree.SubElement(head, "date").text = date
        etree.SubElement(head, "service").text = self.client.url
        etree.SubElement(head, "apiversion").text = str(self.client.apiversion)
        etree.SubElement(head, "generator").text = ("icatdump (python-icat %s)" 
                                                    % icat.__version__)
        self.outfile.write(b"""<?xml version="1.0" encoding="utf-8"?>
<icatdata>
""")
        self.outfile.write(etree.tostring(head, pretty_print=True))

    def startdata(self):
        """Start a new data chunk.

        If the current chunk contains any data, write it to the data
        file.
        """
        if len(self.data) > 0:
            self.outfile.write(etree.tostring(self.data, pretty_print=True))
        self.data = etree.Element("data")

    def writeobj(self, key, obj, keyindex):
        """Add an entity object to the current data chunk."""
        elem = _entity2elem(obj, None, keyindex)
        elem.set('id', key)
        self.data.append(elem)

    def finalize(self):
        """Finalize the data file."""
        self.startdata()
        self.outfile.write(b"</icatdata>\n")
        self.outfile.close()


icat.dumpfile.register_backend("XML", XMLDumpFileReader, XMLDumpFileWriter)
