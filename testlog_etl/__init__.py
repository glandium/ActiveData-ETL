# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import unicode_literals
import re
import math
from pyLibrary import strings
from pyLibrary.dot import Dict, wrap
from pyLibrary.parsers import Log
from pyLibrary.queries import qb


def key2etl(key):
    """
    CONVERT S3 KEY TO ETL HEADER

    S3 NAMING CONVENTION: a.b.c WHERE EACH IS A STEP IN THE ETL PROCESS
    HOW TO DEAL WITH a->b AS AGGREGATION?  (b:a).c?   b->c is agg: c:(a.b)
    NUMBER OF COMBINATIONS IS 2^n, SO PARENTHESIS MUST BE USED

    SPECIAL CASE b:a.c.d WAS MEANT TO BE (b:a).c.d, BUT THERE WAS A BUG

    """
    if key.endswith(".json"):
        key = key[:-5]

    tokens = []
    s = 0
    i = strings.find(key, [":", "."])
    while i < len(key):
        tokens.append(key[s:i])
        tokens.append(key[i])
        s = i + 1
        i = strings.find(key, [":", "."], s)
    tokens.append(key[s:i])
    return wrap(_parse_key(tokens))


def _parse_key(elements):
    """
    EXPECTING ALTERNATING LIST OF operands AND operators
    """
    if isinstance(elements, basestring):
        return {"id": int(elements)}
    if isinstance(elements, list) and len(elements) == 1:
        if isinstance(elements[0], basestring):
            return {"id": int(elements[0])}
        return elements[0]
    if isinstance(elements, dict):
        return elements

    for i in reversed(range(1, len(elements), 2)):
        if elements[i] == ":":
            return _parse_key(elements[:i - 1:] + [{"id": int(elements[i - 1]), "source": _parse_key(elements[i + 1]), "type": "aggregation"}] + elements[i + 2::])
    for i in range(1, len(elements), 2):
        if elements[i] == ".":
            return _parse_key(elements[:i - 1:] + [{"id": int(elements[i + 1]), "source": _parse_key(elements[i - 1]), "type": "join"}] + elements[i + 2::])
    Log.error("Do not know how to parse")


def etl2key(etl):
    if etl.source:
        if etl.source.type:
            if etl.type == etl.source.type:
                if etl.type == "join":
                    return etl2key(etl.source) + "." + unicode(etl.id)
                else:
                    return unicode(etl.id) + ":" + etl2key(etl.source)
            else:
                if etl.type == "join":
                    return "(" + etl2key(etl.source) + ")." + unicode(etl.id)
                else:
                    return unicode(etl.id) + ":(" + etl2key(etl.source) + ")"
        else:
            if etl.type == "join":
                return etl2key(etl.source) + "." + unicode(etl.id)
            else:
                return unicode(etl.id) + ":" + etl2key(etl.source)
    else:
        return unicode(etl.id)


def etl2path(etl):
    """
    CONVERT ETL TO A KEY PREFIX PATH
    """
    try:
        path = []
        while etl:
            path.append(etl.id)
            while etl.type and etl.type != "join":
                etl = etl.source
            etl = etl.source
        return qb.reverse(path)
    except Exception, e:
        Log.error("Can not get path {{etl}}", {"etl": etl}, e)


from . import transforms
