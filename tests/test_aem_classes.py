"""`pdfval classes`: the map named by path or XML-editor URL, its topic references, and the outputclass
values read from a topic."""
from pdfval import aem_classes as c

MAP = "/content/dam/benq-aem-guides/en/Education/BenQ-Board/installation_handbook/Maps/installation_handbook.ditamap"


def test_map_path_from_the_editor_url():
    url = ("http://host:4502/libs/fmdita/clientlibs/xmleditor/page.html?leftPanel=map_panel&src=%2Fcontent%2Fdam%2Fx.ditamap"
           "&ditamap=" + MAP.replace("/", "%2F"))
    assert c.map_path(url) == MAP and c.map_path(MAP) == MAP


def test_topic_and_submap_references():
    xml = ('<map><topicref href="GUID-a-en.dita" type="topic"><topicref href="sub/GUID-b-en.dita#x"/></topicref>'
           '<mapref href="other.ditamap" format="ditamap"/><topicref href="https://benq.com/a.dita" scope="external"/></map>')
    assert c._refs(xml) == (["GUID-a-en", "GUID-b-en"], ["other.ditamap"])


def test_classes_of_a_topic():
    xml = ('<topic id="t"><!-- <p outputclass="gone"/> --><body><p outputclass="p">x</p>'
           '<entry outputclass="entry  img-w60"/><image href="a.png"/></body></topic>')
    got = {(el, at.get("outputclass")) for el, at in c._elements(xml)}
    assert ("p", "p") in got and ("entry", "entry  img-w60") in got and ("image", None) in got
    assert not any(v == "gone" for _, v in got)
