import os
from logging import getLogger
from typing import Tuple, Union, List, Optional

from lxml import etree
from lxml.etree import _ElementTree, _Element

XSD_FILE_PATH = os.path.join(os.path.dirname(__file__), "..", "schemes", "drivebuild.xsd")
SCHEMA_ROOT = etree.parse(XSD_FILE_PATH)
SCHEMA = etree.XMLSchema(SCHEMA_ROOT)
PARSER = etree.XMLParser(schema=SCHEMA, recover=False, remove_comments=True)
NAMESPACES = {
    "db": "http://drivebuild.com"
}
_logger = getLogger("DriveBuild.SimNode.Util.XML")


def validate(path: str) -> Tuple[bool, Optional[_ElementTree]]:
    from util import is_dbe, is_dbc
    from lxml.etree import XMLSyntaxError
    valid: bool = False
    parsed: Optional[_ElementTree] = None
    try:
        parsed = etree.parse(path, PARSER)  # May throw XMLSyntaxException
        if is_dbe(parsed) or is_dbc(parsed):
            valid = SCHEMA.validate(parsed)
    except XMLSyntaxError:
        _logger.exception("Parsing \"" + path + "\" failed")
        valid = False
    return valid, parsed


def xpath(xml_tree: Union[_Element, _ElementTree], expression: str) -> Union[List[_Element], _ElementTree]:
    return xml_tree.xpath(expression, namespaces=NAMESPACES)


def has_tag(node: _Element, namespace: Optional[str], tag_name: str) -> bool:
    if namespace in NAMESPACES:
        if namespace is None:
            prefix = ""
        else:
            prefix = "{" + NAMESPACES[namespace] + "}"
        return node.tag == (prefix + tag_name)
    else:
        raise ValueError("There is no namespace " + namespace)


def get_tag_name(node: _Element) -> str:
    return node.tag.split("}")[1] if "}" in node.tag else node.tag
