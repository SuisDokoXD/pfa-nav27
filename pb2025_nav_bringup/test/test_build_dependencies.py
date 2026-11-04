#!/usr/bin/env python3
"""Consistency checks between CMakeLists.txt and package.xml build tool deps.

Pure Python stdlib only (no ROS, no `launch`, no `ament_*` imports), so this
module runs without a ROS environment.

Why it exists: this package calls `find_package(ament_cmake_auto REQUIRED)` and
`ament_auto_package()` in CMakeLists.txt, so package.xml must declare
`ament_cmake_auto` as a `<buildtool_depend>`. The checks read the real files and
then prove they can fail: the negative-control tests start from a minimal XML
fixture and remove, duplicate or blank the declaration with ElementTree, so a
green run is not merely a restatement of the current file content.

Not registered with colcon/ament -- CMakeLists.txt stays untouched. Run it by
hand, pointing TMPDIR at an existing writable directory you own (the tests
create and delete one subdirectory inside it; no orphan temp dirs):

    TMPDIR=/path/to/existing/scratch PYTHONDONTWRITEBYTECODE=1 \
        python3 -B test/test_build_dependencies.py

or from anywhere:

    TMPDIR=/path/to/existing/scratch python3 -B <package>/test/test_build_dependencies.py -v
"""

import re
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CMAKELISTS = PACKAGE_ROOT / "CMakeLists.txt"
MANIFEST = PACKAGE_ROOT / "package.xml"

# Build tools CMakeLists.txt requires, so package.xml must declare them exactly
# once. Grounded in the real CMakeLists.txt by
# test_cmake_lists_finds_required_buildtools / test_cmake_lists_uses_ament_auto_package.
REQUIRED_BUILDTOOL_DEPENDS = ("ament_cmake_auto",)

# Build tools whose existing declaration must survive.
PRESERVED_BUILDTOOL_DEPENDS = ("ament_cmake",)

EXPECTED_BUILD_TYPE = "ament_cmake"

# Minimal placeholder manifest, used only as a fixture. Deliberately not a copy of
# the real manifest: negative controls must not depend on its exact text.
FIXTURE_MANIFEST = """<?xml version="1.0"?>
<package format="3">
  <name>pb2025_nav_bringup</name>
  <version>1.3.2</version>
  <description>Test fixture, not the real manifest.</description>
  <maintainer email="noreply@example.invalid">Fixture</maintainer>
  <license>Apache-2.0</license>
  <buildtool_depend>ament_cmake</buildtool_depend>
  <buildtool_depend>ament_cmake_auto</buildtool_depend>
  <export>
    <build_type>ament_cmake</build_type>
  </export>
</package>
"""

_FIND_PACKAGE = re.compile(r"^\s*find_package\s*\(\s*([A-Za-z0-9_]+)", re.MULTILINE)
_PROJECT = re.compile(r"^\s*project\s*\(\s*([A-Za-z0-9_]+)", re.MULTILINE)
_AMENT_AUTO_PACKAGE = re.compile(r"^\s*ament_auto_package\s*\(", re.MULTILINE)


def _read(source):
    return Path(source).read_text(encoding="utf-8")


def find_package_names(source):
    """Names passed to find_package() in a CMakeLists.txt file."""
    return {match.group(1) for match in _FIND_PACKAGE.finditer(_read(source))}


def project_name(source):
    """First argument of project() in a CMakeLists.txt file."""
    match = _PROJECT.search(_read(source))
    if match is None:
        raise AssertionError("no project() call in {}".format(source))
    return match.group(1)


def uses_ament_auto_package(source):
    """True when the file invokes ament_auto_package()."""
    return _AMENT_AUTO_PACKAGE.search(_read(source)) is not None


def buildtool_depends(manifest_path):
    """Values of the top-level <buildtool_depend> tags, in document order.

    A tag without text (e.g. `<buildtool_depend/>`) yields "" so callers never
    have to handle None.
    """
    root = ET.parse(str(manifest_path)).getroot()
    return [(element.text or "").strip() for element in root.findall("buildtool_depend")]


def build_type(manifest_path):
    """Value of <export><build_type>, or None when absent or empty."""
    root = ET.parse(str(manifest_path)).getroot()
    element = root.find("export/build_type")
    if element is None:
        return None
    return (element.text or "").strip() or None


def manifest_problems(manifest_path, required, expected_build_type):
    """Human-readable problems with a manifest's build tool declarations.

    `required` names must each appear exactly once; every <buildtool_depend>
    must carry a value; <build_type> must match `expected_build_type`.
    """
    problems = []
    depends = buildtool_depends(manifest_path)

    empty = depends.count("")
    if empty:
        problems.append("empty <buildtool_depend> element ({} found)".format(empty))

    for name in required:
        count = depends.count(name)
        if count == 0:
            problems.append("missing <buildtool_depend>{}</buildtool_depend>".format(name))
        elif count > 1:
            problems.append(
                "duplicate <buildtool_depend>{}</buildtool_depend> ({} times)".format(name, count)
            )

    actual_build_type = build_type(manifest_path)
    if actual_build_type != expected_build_type:
        problems.append(
            "unexpected <build_type>: expected {!r}, found {!r}".format(
                expected_build_type, actual_build_type
            )
        )
    return problems


def remove_buildtool(root, name):
    """Remove the single <buildtool_depend> holding `name`."""
    matches = [element for element in root.findall("buildtool_depend")
               if (element.text or "").strip() == name]
    if len(matches) != 1:
        raise AssertionError(
            "fixture expected exactly one {} <buildtool_depend>, found {}".format(
                name, len(matches)
            )
        )
    root.remove(matches[0])


def duplicate_buildtool(root, name):
    """Insert a second <buildtool_depend> holding `name`, right after the first."""
    for index, element in enumerate(list(root)):
        if element.tag == "buildtool_depend" and (element.text or "").strip() == name:
            clone = ET.Element("buildtool_depend")
            clone.text = name
            clone.tail = element.tail
            root.insert(index + 1, clone)
            return
    raise AssertionError("fixture has no {} <buildtool_depend>".format(name))


def empty_buildtool(root, name):
    """Strip the text of the <buildtool_depend> holding `name` (`<tag/>`)."""
    for element in root.findall("buildtool_depend"):
        if (element.text or "").strip() == name:
            element.text = None
            return
    raise AssertionError("fixture has no {} <buildtool_depend>".format(name))


class ManifestFixture(unittest.TestCase):
    """Writes throwaway manifests derived from FIXTURE_MANIFEST."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="test_build_deps_")
        self.addCleanup(shutil.rmtree, self._tmpdir, ignore_errors=True)

    def write_fixture(self, mutate=None, indent=None):
        tree = ET.ElementTree(ET.fromstring(FIXTURE_MANIFEST))
        if mutate is not None:
            mutate(tree.getroot())
        if indent is not None:
            ET.indent(tree, space=indent)
        path = Path(self._tmpdir) / "package.xml"
        tree.write(str(path), encoding="utf-8", xml_declaration=True)
        return path


class TestRealPackage(unittest.TestCase):
    def test_manifest_parses_as_xml(self):
        root = ET.parse(str(MANIFEST)).getroot()
        self.assertEqual(root.tag, "package")
        self.assertEqual(root.get("format"), "3")

    def test_manifest_name_matches_cmake_project(self):
        root = ET.parse(str(MANIFEST)).getroot()
        self.assertEqual(
            root.findtext("name"),
            project_name(CMAKELISTS),
            "package.xml <name> must match project() in {}".format(CMAKELISTS.name),
        )

    def test_cmake_lists_finds_required_buildtools(self):
        """Grounds REQUIRED_BUILDTOOL_DEPENDS in the real build description."""
        found = find_package_names(CMAKELISTS)
        for name in REQUIRED_BUILDTOOL_DEPENDS:
            self.assertIn(
                name,
                found,
                "{} is not find_package()'d in {}; the manifest check would "
                "be vacuous".format(name, CMAKELISTS.name),
            )

    def test_cmake_lists_uses_ament_auto_package(self):
        """ament_auto_package() is what consumes ament_cmake_auto."""
        self.assertTrue(
            uses_ament_auto_package(CMAKELISTS),
            "{} does not call ament_auto_package()".format(CMAKELISTS.name),
        )

    def test_manifest_declares_buildtools_once(self):
        required = REQUIRED_BUILDTOOL_DEPENDS + PRESERVED_BUILDTOOL_DEPENDS
        self.assertEqual(
            manifest_problems(MANIFEST, required, EXPECTED_BUILD_TYPE),
            [],
            "{}: <buildtool_depend> entries = {}".format(
                MANIFEST.name, buildtool_depends(MANIFEST)
            ),
        )


class TestCheckerDetectsRegressions(ManifestFixture):
    """Negative controls: the checker must fail on broken fixtures."""

    def assert_reports(self, manifest_path, required, needle):
        problems = manifest_problems(manifest_path, required, EXPECTED_BUILD_TYPE)
        self.assertTrue(problems, "checker reported no problem for {}".format(manifest_path))
        self.assertTrue(
            any(needle in problem for problem in problems),
            "no problem mentioned {!r}; got {}".format(needle, problems),
        )
        return problems

    def test_fixture_baseline_passes(self):
        """The fixture machinery must not invent problems."""
        required = REQUIRED_BUILDTOOL_DEPENDS + PRESERVED_BUILDTOOL_DEPENDS
        fixture = self.write_fixture()
        self.assertEqual(manifest_problems(fixture, required, EXPECTED_BUILD_TYPE), [])

    def test_missing_ament_cmake_auto_is_reported(self):
        fixture = self.write_fixture(lambda root: remove_buildtool(root, "ament_cmake_auto"))
        self.assert_reports(
            fixture,
            REQUIRED_BUILDTOOL_DEPENDS,
            "missing <buildtool_depend>ament_cmake_auto</buildtool_depend>",
        )

    def test_missing_ament_cmake_is_reported(self):
        fixture = self.write_fixture(lambda root: remove_buildtool(root, "ament_cmake"))
        self.assert_reports(
            fixture,
            PRESERVED_BUILDTOOL_DEPENDS,
            "missing <buildtool_depend>ament_cmake</buildtool_depend>",
        )

    def test_duplicate_ament_cmake_auto_is_reported(self):
        fixture = self.write_fixture(lambda root: duplicate_buildtool(root, "ament_cmake_auto"))
        self.assert_reports(
            fixture,
            REQUIRED_BUILDTOOL_DEPENDS,
            "duplicate <buildtool_depend>ament_cmake_auto</buildtool_depend>",
        )

    def test_empty_buildtool_depend_is_reported(self):
        """A text-less <buildtool_depend/> must be reported, not raise."""
        fixture = self.write_fixture(lambda root: empty_buildtool(root, "ament_cmake_auto"))
        problems = self.assert_reports(
            fixture, REQUIRED_BUILDTOOL_DEPENDS, "empty <buildtool_depend> element"
        )
        self.assertIn(
            "missing <buildtool_depend>ament_cmake_auto</buildtool_depend>",
            problems,
            "an empty declaration must count as missing, got {}".format(problems),
        )

    def test_indentation_is_irrelevant(self):
        """Reindented manifests are judged the same way."""
        required = REQUIRED_BUILDTOOL_DEPENDS + PRESERVED_BUILDTOOL_DEPENDS
        for indent in ("    ", "\t", ""):
            with self.subTest(indent=repr(indent)):
                fixture = self.write_fixture(indent=indent)
                self.assertIn("ament_cmake_auto", buildtool_depends(fixture))
                self.assertEqual(manifest_problems(fixture, required, EXPECTED_BUILD_TYPE), [])


if __name__ == "__main__":
    unittest.main()
