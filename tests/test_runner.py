
from app.runner import parse_junit


def test_junit_records_pass_fail_skipped_and_errors(tmp_path):
    (tmp_path / "junit.xml").write_text(
        '''<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite tests="4" failures="1" errors="1" skipped="1">
 <testcase classname="demo" name="test_ok" time="0.1"/>
 <testcase classname="demo" name="test_bad" time="0.2"><failure message="assert 1 == 2">failure</failure></testcase>
 <testcase classname="demo" name="test_error" time="0.0"><error message="import failed"/></testcase>
 <testcase classname="demo" name="test_skip"><skipped/></testcase>
</testsuite></testsuites>''', encoding="utf-8"
    )
    cases = parse_junit(tmp_path)
    assert [c["outcome"] for c in cases] == ["passed", "failed", "error", "skipped"]
    assert cases[1]["details"] == "assert 1 == 2"


def test_junit_refuses_symlink_and_xml_entity(tmp_path):
    other = tmp_path / "unsafe.xml"
    other.write_text('<!DOCTYPE foo [ <!ENTITY boom SYSTEM "file:///etc/passwd"> ]><testcase>&boom;</testcase>')
    junit = tmp_path / "junit.xml"
    junit.symlink_to(other)
    assert parse_junit(tmp_path) == []
    junit.unlink()
    junit.write_bytes(other.read_bytes())
    assert parse_junit(tmp_path) == []
