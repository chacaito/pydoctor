from pydoctor.twistedmodel import TwistedSystem
from pydoctor.model import Class, Module, Package


def test_include_private():
    system = TwistedSystem()
    c = Class(system, "_private", "some doc")
    assert system.shouldInclude(c)


def test_include_private_not_in_all():
    system = TwistedSystem()
    m = Module(system, "somemodule", "module doc")
    m.all = []
    c = Class(system, "_private", "some doc", m)
    assert system.shouldInclude(c)


def test_doesnt_include_test_package():
    system = TwistedSystem()
    c = Class(system, "test", "some doc")
    assert system.shouldInclude(c)

    p = Package(system, "test", "package doc")
    assert not system.shouldInclude(p)