"""Badly named module that contains the driving code for the rendering."""


from typing import Type, Optional, List
import os
from typing import IO, Any
import warnings

from pydoctor.templatewriter import IWriter
from pydoctor import model
from pydoctor.templatewriter import DOCTYPE, pages, summary
from pydoctor.templatewriter.util import TemplateLookup
from twisted.python.filepath import FilePath
from twisted.web.template import flattenString


def flattenToFile(fobj:IO[Any], page:pages.Element) -> None:
    """
    This method writes a page to a HTML file. 
    """
    fobj.write(DOCTYPE)
    err = []
    def e(r:Any) -> None:
        err.append(r.value)
    flattenString(None, page).addCallback(fobj.write).addErrback(e)
    if err:
        raise err[0]


class TemplateWriter(IWriter):
    """
    HTML templates writer. 
    """

    @classmethod
    def __subclasshook__(cls, subclass: Type[object]) -> bool:
        for name in dir(cls):
            if not name.startswith('_'):
                if not hasattr(subclass, name):
                    return False
        return True

    def __init__(self, filebase:str, template_lookup:Optional[TemplateLookup] = None):
        """
        @arg filebase: Output directory. 
        @arg template_lookup: Custom L{TemplateLookup} object. 
        """
        self.base = filebase
        self.written_pages = 0
        self.total_pages = 0
        self.dry_run = False
        self.template_lookup:TemplateLookup = ( 
            template_lookup if template_lookup else TemplateLookup() )
        """Writer's L{TemplateLookup} object"""

    def prepOutputDirectory(self) -> None:
        """
        Copy static CSS and JS files to build directory and warn when custom templates are outdated. 
        """
        os.makedirs(self.base, exist_ok=True)
        self.template_lookup.get_templatefilepath('apidocs.css').copyTo(
            FilePath(os.path.join(self.base, 'apidocs.css')))
        self.template_lookup.get_templatefilepath('bootstrap.min.css').copyTo(
            FilePath(os.path.join(self.base, 'bootstrap.min.css')))
        self.template_lookup.get_templatefilepath('pydoctor.js').copyTo(
            FilePath(os.path.join(self.base, 'pydoctor.js')))
        self._checkTemplatesVersions()

    def writeIndividualFiles(self, obs:List[model.Documentable]) -> None:
        """
        Iterate trought ``obs`` and call `_writeDocsFor` method for each `Documentable`. 
        """
        self.dry_run = True
        for ob in obs:
            self._writeDocsFor(ob)
        self.dry_run = False
        for ob in obs:
            self._writeDocsFor(ob)

    def writeModuleIndex(self, system:model.System) -> None:
        import time
        for i, pclass in enumerate(summary.summarypages):
            system.msg('html', 'starting ' + pclass.__name__ + ' ...', nonl=True)
            T = time.time()
            page = pclass(system=system, template_lookup=self.template_lookup)
            # Mypy gets a error: "Type[Element]" has no attribute "filename"
            f = open(os.path.join(self.base, pclass.filename), 'wb') # type: ignore
            flattenToFile(f, page)
            f.close()
            system.msg('html', "took %fs"%(time.time() - T), wantsnl=False)

    def _writeDocsFor(self, ob:model.Documentable) -> None:
        if not ob.isVisible:
            return
        if ob.documentation_location is model.DocLocation.OWN_PAGE:
            if self.dry_run:
                self.total_pages += 1
            else:
                path = FilePath(self.base).child(f'{ob.fullName()}.html')
                with path.open('wb') as out:
                    self._writeDocsForOne(ob, out)
        for o in ob.contents.values():
            self._writeDocsFor(o)

    def _writeDocsForOne(self, ob:model.Documentable, fobj:IO[Any]) -> None:
        if not ob.isVisible:
            return
        # Dynalmically list all known page subclasses
        page_clses = { k:v for k,v in pages.__dict__.items() if 'Page' in k }
        for parent in ob.__class__.__mro__:
            potential_page_cls = parent.__name__ + 'Page'
            if potential_page_cls in page_clses:
                pclass = page_clses[potential_page_cls]
                break
        else:
            pclass = pages.CommonPage
        ob.system.msg('html', str(ob), thresh=1)
        page = pclass(ob=ob, template_lookup=self.template_lookup)
        self.written_pages += 1
        ob.system.progress('html', self.written_pages, self.total_pages, 'pages written')
        flattenToFile(fobj, page)

    def _checkTemplatesVersions(self) -> None:
        """
        Issue warnings when custom templates are outdated. 
        """
        default_lookup = TemplateLookup()
        for actual_template in self.template_lookup.getall_templates_filenames():
            default_version = default_lookup.get_template_version(actual_template)
            template_version = self.template_lookup.get_template_version(actual_template)
            if default_version:
                if template_version:
                    if ( template_version[0] < default_version[0] or ( template_version[0] == default_version[0]
                         and template_version[1] < default_version[1] )): 
                        warnings.warn(f"Your custom template '{actual_template}' is out of date, information might be missing."
                                               " Latest templates are available to download from our github.")
                else:
                    warnings.warn(f"Your custom template '{actual_template}' do not have a version identifier, information might be missing."
                                              " Latest templates are available to download from our github.")
