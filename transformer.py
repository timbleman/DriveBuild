from typing import List

from lxml.etree import _ElementTree

from db_types import ScenarioMapping, TestCase


def get_author(root: _ElementTree) -> str:
    from xml_util import xpath
    return xpath(root, "db:author")[0].text


def transform(mappings: List[ScenarioMapping]) -> List[TestCase]:
    from generator import generate_scenario
    from kp_transformer import generate_criteria
    from xml_util import xpath
    test_cases = list()
    for mapping in mappings:
        environment = mapping.environment
        environment_author = get_author(environment)
        for crit_def in mapping.crit_defs:
            participants_node = xpath(crit_def, "db:participants")[0]
            builder = generate_scenario(environment, participants_node)
            criteria = generate_criteria(crit_def)
            crit_def_author = get_author(crit_def)
            authors = [environment_author]
            if crit_def_author not in authors:
                authors.append(crit_def_author)
            test_cases.append(TestCase(builder, criteria, authors))
    return test_cases
