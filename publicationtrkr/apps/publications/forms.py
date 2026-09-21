import re

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.utils.bibtex_utils import (
    normalize_author_name,
    parse_bibtex,
)
from publicationtrkr.apps.publications.utils.display_names import account_name_for, automatic_display_name
from publicationtrkr.apps.publications.utils.publication_builder import resolve_create_fields
from publicationtrkr.utils.names import check_account_name


# The separators a human can use between authors. A comma is deliberately NOT one of
# them: it is the separator inside a single BibTeX name ("Grigoryan, Garegin"), so
# splitting on it turned one author into two -- which is half of #66 and most of what #62
# had to repair. Newline is the documented shape; semicolon is accepted because a user
# faced with the old comma-splitting field already reached for it as a workaround, and
# that improvised convention is now stored on e7742605.
AUTHOR_SEPARATORS = re.compile(r'[\n\r;]+')


def split_author_lines(value: str) -> list:
    """
    One author per line, in the order given, un-inverting any "Last, First" line.

    The un-inversion matters for agreement rather than for tidiness: the same publication
    can be built from this field or from a pasted BibTeX entry, and resolve_create_fields
    merges the two. If only one side un-inverted, the stored spelling would depend on
    which box the user happened to type in.
    """
    if not value:
        return []
    names = []
    for line in AUTHOR_SEPARATORS.split(value):
        name = normalize_author_name(line)
        if name:
            names.append(name)
    return names


class PublicationForm(forms.ModelForm):
    """
    {
      "authors": [
        "string"
      ],
      "link": "string",
      "project_name": "string",
      "project_uuid": "string",
      "title": "string",
      "venue": "string",
      "year": "string"
    }
    """
    required_css_class = 'required'

    bibtex = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 8, 'cols': 60}),
        required=False,
        label='BibTeX (optional - paste to auto-fill fields below)',
    )

    title = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=False,
        label='Title *',
    )

    link = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=False,
        label='Link (optional)',
    )

    year = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=False,
        label='Year *',
    )

    venue = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=False,
        label='Venue',
    )

    authors = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 6, 'cols': 60}),
        required=False,
        label='Authors -- one per line, in the order they appear on the publication *',
    )

    project_name = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=False,
        label='Project Name (optional)',
    )

    project_uuid = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=False,
        label='Project UUID (optional, required if project_name is provided)',
    )

    def __init__(self, *args, **kwargs):
        authors = kwargs.pop('authors', [])
        super().__init__(*args, **kwargs)
        self.initial['authors'] = '\n'.join(str(a) for a in authors)

    def clean(self):
        cleaned_data = super().clean()

        # parse bibtex if provided
        bibtex_string = cleaned_data.get('bibtex', '')
        bibtex_data = parse_bibtex(bibtex_string) if bibtex_string else {}

        # The form's own input shape: one author per line. Everything after that is the
        # shared "manual overrides BibTeX" merge, so the form and the API cannot drift
        # again.
        manual = {name: value.strip() if isinstance(value, str) else value
                  for name, value in cleaned_data.items()}
        manual['authors'] = split_author_lines(manual.get('authors', ''))

        resolved = resolve_create_fields(manual, bibtex_data)

        # Only the fields this form is responsible for are written back. venue and
        # the project fields pass through untouched: the form validates nothing
        # about them, and the API layer resolves them from the same BibTeX.
        if resolved['authors']:
            cleaned_data['authors'] = resolved['authors']
        else:
            self.add_error(None, 'Authors: must provide at least one author directly or via BibTeX.')

        if resolved['title']:
            cleaned_data['title'] = resolved['title']
        else:
            self.add_error(None, 'Title: must provide a title directly or via BibTeX.')

        # link is http(s) only -- it is rendered into an href, where 'javascript:'
        # would run in the page origin.
        link = resolved['link']
        if link:
            try:
                URLValidator(schemes=['http', 'https'])(str(link).strip())
                cleaned_data['link'] = str(link).strip()
            except ValidationError:
                self.add_error(None, 'Link: must be a valid http:// or https:// URL.')
        else:
            cleaned_data['link'] = ''

        if resolved['year']:
            cleaned_data['year'] = resolved['year']
        else:
            self.add_error(None, 'Year: must provide a year directly or via BibTeX.')

        return cleaned_data

    class Meta:
        model = Publication
        fields = ['bibtex', 'title', 'authors', 'link', 'year', 'venue', 'project_name', 'project_uuid']


class AuthorForm(forms.ModelForm):
    """
    Form for editing an Author record.

    Permission levels:
    - can_create_publication (or is_publication_tracker_admin):
        May edit display_name only. fabric_uuid is set automatically to api_user.uuid.
    - is_publication_tracker_admin:
        May additionally edit author_name, publication_uuid, and fabric_uuid.
        The author's uuid field is never editable.

    `use_account_name` is the "use my FABRIC name" choice (#73). Checked, the author shows
    the credited person's account name and follows later changes to it. Unchecking it
    keeps the name shown, as a custom name. A name typed into display_name is a choice in
    itself, whatever the box says. clean() turns that into mutate_author's tri-state.
    """
    required_css_class = 'required'

    correction_reason = forms.CharField(
        required=False, max_length=2000,
        widget=forms.Textarea(attrs={'rows': 2, 'cols': 60}),
        label='Reason for attribution correction',
        help_text='Required when changing or removing an existing attribution. Prior claims are retained in the correction history.',
    )

    display_name = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=True,
        label='Display Name *',
    )

    use_account_name = forms.BooleanField(required=False)

    # Admin-only fields
    author_name = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=False,
        label='Author Name *',
    )

    publication_uuid = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=False,
        label='Publication UUID *',
    )

    fabric_uuid = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=False,
        label='FABRIC UUID (optional)',
    )

    def __init__(self, *args, **kwargs):
        self.api_user = kwargs.pop('api_user', None)
        self.is_admin = bool(self.api_user and self.api_user.is_publication_tracker_admin)
        super().__init__(*args, **kwargs)
        if not self.is_admin:
            del self.fields['correction_reason']
            del self.fields['author_name']
            del self.fields['publication_uuid']
            del self.fields['fabric_uuid']
        self._init_use_account_name()

    def _init_use_account_name(self):
        field = self.fields['use_account_name']
        self.account_name_note = None
        if self.is_admin:
            field.label = "Follow the credited person's FABRIC account name"
            field.help_text = (
                'Checked: shows their account name when it is usable, otherwise the byline, '
                'and follows later changes. Unchecked: keeps the name above.')
            account_name = account_name_for(self.instance.fabric_uuid)
            if account_name:
                field.label += ' ({0})'.format(account_name)
            # Checked only when the row already shows what it would: an admin editing some
            # other field of an author credited before #73 must not switch its name as a
            # side effect. sync_author_display_names, reviewed, does that.
            following = (self.instance.display_name, self.instance.display_name_source) \
                == automatic_display_name(self.instance, account_name)
        else:
            # A claimant's own account name is known before they save, so an unusable one
            # is explained rather than offered.
            account_name, reason = check_account_name(self.api_user.name if self.api_user else None)
            if account_name is None:
                del self.fields['use_account_name']
                self.account_name_note = (
                    'Your FABRIC account name cannot be shown on papers as it is ({0}), so '
                    'this author keeps the name above. You can change your name in the '
                    'FABRIC portal.'.format(reason))
                return
            field.label = 'Show my FABRIC account name ({0})'.format(account_name)
            field.help_text = (
                'Checked: this paper shows your FABRIC account name and follows it when you '
                'change it in the FABRIC portal. Unchecked: keeps the name above.')
            # Claiming credits the author, and crediting follows the account name unless
            # the name is custom -- so that is what the box says before saving.
            following = self.instance.display_name_source != Author.CUSTOM
        self.initial.setdefault('use_account_name', following)

    def clean(self):
        cleaned_data = super().clean()

        display_name = cleaned_data.get('display_name', '').strip()
        if display_name:
            cleaned_data['display_name'] = display_name

        if 'use_account_name' in self.fields:
            if 'display_name' in self.changed_data:
                cleaned_data['use_account_name'] = None
            elif cleaned_data.get('use_account_name'):
                cleaned_data['use_account_name'] = True
            elif 'use_account_name' in self.changed_data:
                cleaned_data['use_account_name'] = False
            else:
                cleaned_data['use_account_name'] = None

        if self.is_admin:
            # author_name is required
            author_name = cleaned_data.get('author_name', '').strip()
            if not author_name:
                self.add_error('author_name', 'Author name is required.')
            else:
                cleaned_data['author_name'] = author_name

            # publication_uuid must reference an existing Publication
            publication_uuid = cleaned_data.get('publication_uuid', '').strip()
            if not publication_uuid:
                self.add_error('publication_uuid', 'Publication UUID is required.')
            elif not Publication.objects.filter(uuid=publication_uuid).exists():
                self.add_error('publication_uuid',
                               'No publication found with UUID: {}'.format(publication_uuid))
            else:
                cleaned_data['publication_uuid'] = publication_uuid

            # fabric_uuid must reference an existing ApiUser if provided
            fabric_uuid = cleaned_data.get('fabric_uuid', '').strip()
            if fabric_uuid:
                if not ApiUser.objects.filter(uuid=fabric_uuid).exists():
                    self.add_error('fabric_uuid',
                                   'No FABRIC user found with UUID: {}'.format(fabric_uuid))
                else:
                    cleaned_data['fabric_uuid'] = fabric_uuid
            else:
                cleaned_data['fabric_uuid'] = None

        return cleaned_data

    class Meta:
        model = Author
        fields = ['display_name', 'author_name', 'publication_uuid', 'fabric_uuid']
