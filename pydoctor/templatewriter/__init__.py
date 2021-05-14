"""Render pydoctor data as HTML."""
from typing import Iterable, Iterator, Optional, Dict, Union, overload, TYPE_CHECKING
if TYPE_CHECKING:
    from typing_extensions import Protocol, runtime_checkable
else:
    Protocol = object
    def runtime_checkable(f):
        return f
import abc
from pathlib import Path, PurePath
from os.path import splitext
import warnings
import sys
from xml.dom import minidom

# Newer APIs from importlib_resources should arrive to stdlib importlib.resources in Python 3.9.
if sys.version_info < (3, 9):
    import importlib_resources
    from importlib_resources.abc import Traversable
else:
    import importlib.resources as importlib_resources
    from importlib.abc import Traversable

from twisted.web.iweb import ITemplateLoader
from twisted.web.template import TagLoader, XMLString, Element, tags

from pydoctor.model import System, Documentable

DOCTYPE = b'''\
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"
          "DTD/xhtml1-strict.dtd">
'''

def parse_xml(text: str) -> minidom.Document:
    """
    Create a L{minidom} representaton of the XML string.
    """
    try:
        return minidom.parseString(text)
    except Exception as e:
        raise ValueError(f"Failed to parse template as XML: {e}") from e

def scandir(path: Union[Traversable, Path]) -> Iterator['Template']:
    """
    Scan a directory for templates. 
    """
    for entry in path.iterdir():
        template = Template.fromfile(entry)
        if template:
            yield template

class TemplateError(Exception):
    pass

class UnsupportedTemplateVersion(TemplateError):
    """Raised when custom template is designed for a newer version of pydoctor"""

class OverrideTemplateNotAllowed(TemplateError):
    """Raised when a template is trying to be overriden because of a bogus path entry"""

class FailedToCreateTemplate(TemplateError):
    """Raised when a template could not be created because of an error"""

@runtime_checkable
class IWriter(Protocol):
    """
    Interface class for pydoctor output writer.
    """

    @overload
    def __init__(self, htmloutput: str) -> None: ...
    @overload
    def __init__(self, htmloutput: str, template_lookup: 'TemplateLookup') -> None: ...

    def prepOutputDirectory(self) -> None:
        """
        Called first.
        """

    def writeSummaryPages(self, system: System) -> None:
        """
        Called second.
        """

    def writeIndividualFiles(self, obs: Iterable[Documentable]) -> None:
        """
        Called last.
        """

class Template(abc.ABC):
    """
    Represents a pydoctor template file.

    It holds references to template information.

    It's an additionnal level of abstraction to hook to the
    rendering system. 

    Use L{Template.fromfile} to create Templates.

    @see: L{TemplateLookup}
    """

    def __init__(self, name: str):
        self.name = name
        """Template filename"""

    @classmethod
    def fromfile(cls, path: Union[Traversable, Path]) -> Optional['Template']:
        """
        Create a concrete template object.
        Type depends on the file extension.

        @param path: A L{Path} or L{Traversable} object that should point to a template file or folder. 
        @returns: The template object or C{None} if file extension is invalid.
        @raises FailedToCreateTemplate: If there is an error while creating the template.
        """

        def suffix(name: str) -> str:
            # Workaround to get a filename extension because
            # importlib.abc.Traversable objects do not include .suffix property.
            _, ext = splitext(name)
            return ext

        if path.is_dir():
            return StaticTemplateFolder(name=path.name, 
                                      lookup=TemplateLookup(path))
        if not path.is_file():
            return None
        file_extension = suffix(path.name).lower()
        try:
            if file_extension == '.html':
                try:
                    with path.open('r', encoding='utf-8') as fobj:
                        text = fobj.read()
                except UnicodeDecodeError as e:
                    raise FailedToCreateTemplate("Cannot decode HTML Template"
                                f" as UTF-8: '{path}'. {e}") from e
                else:
                    return HtmlTemplate(name=path.name, text=text)
            else:
                # treat the file as binary data.
                with path.open('rb') as fobjb:
                    _bytes = fobjb.read()
                return StaticTemplateFile(name=path.name, data=_bytes)
        except IOError as e:
            raise FailedToCreateTemplate(f"Cannot read Template: '{path}'."
                        " I/O error: {e}") from e

    @abc.abstractmethod
    def is_empty(self) -> bool:
        """
        Does this template is empty? Emptyness is defined by subclasses. 
        Empty placeholder templates will not be rendered, but 
        """
        raise NotImplementedError()

class StaticTemplate(Template):

    def write(self, output_dir: Path, subfolder: Optional[PurePath] = None) -> PurePath:
        """
        Directly write the contents of this static template as is to the output dir.

        @returns: The relative path of the file that has been wrote.
        """
        _subfolder_path = subfolder if subfolder else PurePath()
        _template_path = _subfolder_path.joinpath(self.name)
        outfile = output_dir.joinpath(_template_path)
        self._write(outfile)
        return _template_path
    
    @abc.abstractmethod
    def _write(self, path: Path) -> None:
        raise NotImplementedError()

class StaticTemplateFile(StaticTemplate):
    """
    Static template: no rendering, will be copied as is to build directory.

    For CSS and JS templates.
    """
    data: bytes
    
    def __init__(self, name: str, data: bytes) -> None:
        super().__init__(name)
        self.data: bytes = data
        """
        Template data: contents of the template file as 
        UFT-8 decoded L{str} or directly L{bytes} for static templates.
        """
    
    def is_empty(self) -> bool:
        return len(self.data)==0
    
    def _write(self, path: Path) -> None:
        with path.open('wb') as fobjb:
            fobjb.write(self.data)

class StaticTemplateFolder(StaticTemplate):
    """
    Special template to hold a subfolder contents. 

    Subfolders should only contains static files. 

    Currently used for C{fonts}.
    """
    def __init__(self, name: str, lookup: 'TemplateLookup'):
        super().__init__(name)

        self.lookup: 'TemplateLookup' = lookup
        """
        The lookup instance that contains the subfolder templates. 
        """

    def write(self, output_dir: Path, subfolder: Optional[PurePath] = None) -> PurePath:
        """
        Create the subfolder and reccursively write it's content to the output directory.
        """
        subfolder = super().write(output_dir, subfolder)
        for template in self.lookup.templates:
            if isinstance(template, StaticTemplate):
                template.write(output_dir, subfolder)
        return subfolder

    def _write(self, path: Path) -> None:
        path.mkdir(exist_ok=True, parents=True)
    
    def is_empty(self) -> bool:
        return len(list(self.lookup._templates))==0
        
class HtmlTemplate(Template):
    """
    HTML template that works with the Twisted templating system
    and use L{xml.dom.minidom} to parse the C{pydoctor-template-version} meta tag.
    """
    data: str

    def __init__(self, name: str, text: str):
        super().__init__(name=name)
        self.data = text
        if self.is_empty():
            self._dom: Optional[minidom.Document] = None
            self._version = -1
            self._loader: ITemplateLoader = TagLoader(tags.transparent)
        else:
            self._dom = parse_xml(self.data)
            self._version = self._extract_version(self._dom, self.name)
            self._loader = XMLString(self._dom.toxml())

    @property
    def version(self) -> int:
        """
        Template version, C{-1} if no version could be read in the XML file.

        HTML Templates should have a version identifier as follow::

            <meta name="pydoctor-template-version" content="1" />

        The version indentifier should be a integer.
        """
        return self._version

    @property
    def loader(self) -> ITemplateLoader:
        """
        Object used to render the final file.

        This is a L{ITemplateLoader}.
        """
        return self._loader
    
    def is_empty(self) -> bool:
        return len(self.data.strip()) == 0

    @staticmethod
    def _extract_version(dom: minidom.Document, template_name: str) -> int:
        # If no meta pydoctor-template-version tag found,
        # it's most probably a placeholder template.
        version = -1
        for meta in dom.getElementsByTagName("meta"):
            if meta.getAttribute("name") != "pydoctor-template-version":
                continue

            # Remove the meta tag as soon as found
            meta.parentNode.removeChild(meta)

            if not meta.hasAttribute("content"):
                warnings.warn(f"Could not read '{template_name}' template version: "
                    f"the 'content' attribute is missing")
                continue

            version_str = meta.getAttribute("content")

            try:
                version = int(version_str)
            except ValueError:
                warnings.warn(f"Could not read '{template_name}' template version: "
                        "the 'content' attribute must be an integer")
            else:
                break

        return version

class TemplateLookup:
    """
    The L{TemplateLookup} handles the HTML template files locations.
    A little bit like C{mako.lookup.TemplateLookup} but more simple.

    The location of the files depends wether the users set a template directory
    with the option C{--template-dir}, custom files with matching names will be
    loaded if present.

    This object allow the customization of any templates, this can lead to warnings
    when upgrading pydoctor, then, please update your template.

    @note: The HTML templates versions are independent of the pydoctor version
           and are idependent from each other.

    @see: L{Template}
    """

    def __init__(self, template_dir: Optional[Union[Traversable, Path]] = None, 
                       theme: str = 'classic') -> None:
        """
        Init L{TemplateLookup} with templates in C{pydoctor/templates}.
        This loads all templates into the lookup C{_templates} dict.

        @param template_dir: A custom L{Path} or L{Traversable} object to load the templates from.
        @param theme: Load the theme if C{template_dir} is not defined.
        """
        self._templates: Dict[str, Template] = {}

        if not template_dir:
            theme_path = importlib_resources.files('pydoctor.themes') / theme
            self.add_templatedir(theme_path)
        else:
            self.add_templatedir(template_dir)
        
        self._default_templates = self._templates.copy()
    
    def _add_overriding_html_template(self, template: HtmlTemplate, current_template: HtmlTemplate) -> None:
        default_version = current_template.version
        template_version = template.version
        if default_version != -1 and template_version != -1:
            if template_version < default_version:
                warnings.warn(f"Your custom template '{template.name}' is out of date, "
                                "information might be missing. "
                                "Latest templates are available to download from our github." )
            elif template_version > default_version:
                raise UnsupportedTemplateVersion(f"It appears that your custom template '{template.name}' "
                                    "is designed for a newer version of pydoctor."
                                    "Rendering will most probably fail. Upgrade to latest "
                                    "version of pydoctor with 'pip install -U pydoctor'. ")
        self._templates[template.name] = template

    def add_template(self, template: Template) -> None:
        """
        Add a template to the lookup. The custom template override the default. 
        
        If the file doesn't exist in the default template, we assume it is additional data used by the custom template.

        For HTML, compare the passed Template version with default template,
        issue warnings if template are outdated.

        @raises UnsupportedTemplateVersion: If the custom template is designed for a newer version of pydoctor.
        @raises OverrideTemplateNotAllowed: If a path in this template overrides a path of a different type (HTML/static/subdir).
        """

        current_template = self._templates.get(template.name, None)
        if current_template:
            if isinstance(current_template, StaticTemplateFolder):
                if isinstance(template, StaticTemplateFolder):
                    for t in template.lookup.templates:
                        current_template.lookup.add_template(t)
                else:
                    raise OverrideTemplateNotAllowed("Cannot override StaticTemplateFolder with "
                        f"a {template.__class__.__name__}. "
                        f"Rename '{template.name}' to something else. ")
            
            elif isinstance(current_template, StaticTemplateFile):
                if isinstance(template, StaticTemplateFile):
                    self._templates[template.name] = template
                else:
                    raise OverrideTemplateNotAllowed(f"Cannot override StaticTemplateFile with "
                        f"a {template.__class__.__name__}. "
                        f"Rename '{template.name}' to something else. ")
            
            elif isinstance(current_template, HtmlTemplate):
                if isinstance(template, HtmlTemplate):
                    self._add_overriding_html_template(template, current_template)
                else:
                    raise OverrideTemplateNotAllowed(f"Cannot override HtmlTemplate with "
                        f"a {template.__class__.__name__}. "
                        f"Rename '{template.name}' to something else. ")
        else:
            self._templates[template.name] = template

    def add_templatedir(self, dir: Union[Path, Traversable]) -> None:
        """
        Scan a directory and add all templates in the given directory to the lookup.
        """
        for template in scandir(dir):
            self.add_template(template)

    def get_template(self, filename: str) -> Template:
        """
        Lookup a template based on its filename.

        Return the custom template if provided, else the default template.

        @param filename: File name, (ie 'index.html')
        @return: The Template object
        @raises KeyError: If no template file is found with the given name
        """
        try:
            t = self._templates[filename]
        except KeyError as e:
            raise KeyError(f"Cannot find template '{filename}' in template lookup: {self}. "
                f"Valid filenames are: {list(self._templates)}") from e
        return t

    def get_loader(self, filename: str) -> ITemplateLoader:
        """
        Lookup a HTML template loader based on its filename.

        @raises ValueError: If the template loader is C{None}.
        """ 
        template = self.get_template(filename)
        if not isinstance(template, HtmlTemplate):
            raise ValueError(f"Failed to get loader of template '{filename}': Not a HTML template.")
        return template.loader

    @property
    def templates(self) -> Iterable[Template]:
        """
        All templates that can be looked up.
        For each name, the custom template will be included if it exists,
        otherwise the default template.
        """
        return self._templates.values()

class TemplateElement(Element, abc.ABC):
    """
    Renderable element based on a template file.
    """

    filename: str = NotImplemented
    """
    Associated template filename.
    """

    @classmethod
    def lookup_loader(cls, template_lookup: TemplateLookup) -> ITemplateLoader:
        """
        Lookup the element L{ITemplateLoader} with the C{TemplateLookup}.
        """
        return template_lookup.get_loader(cls.filename)

from pydoctor.templatewriter.writer import TemplateWriter
__all__ = ["TemplateWriter"] # re-export as pydoctor.templatewriter.TemplateWriter
