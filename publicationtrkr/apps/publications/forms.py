from django import forms
from django.forms import CheckboxSelectMultiple

from publicationtrkr.apps.publications.models import Author, Publication
from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.utils.bibtex_utils import parse_bibtex


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
        widget=forms.Textarea(attrs={'rows': 3, 'cols': 60}),
        required=False,
        label='Authors (comma separated) *',
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
        self.initial['authors'] = ', '.join(str(a) for a in authors)

    def clean(self):
        cleaned_data = super().clean()

        # parse bibtex if provided
        bibtex_string = cleaned_data.get('bibtex', '')
        bibtex_data = {}
        if bibtex_string:
            bibtex_data = parse_bibtex(bibtex_string)

        # resolve authors: manual input overrides bibtex
        authors_raw = cleaned_data.get('authors', '').strip()
        if authors_raw:
            cleaned_data['authors'] = [a.strip() for a in authors_raw.split(',') if a.strip()]
        elif bibtex_data.get('authors'):
            cleaned_data['authors'] = bibtex_data['authors']
        else:
            self.add_error(None, 'Authors: must provide at least one author directly or via BibTeX.')

        # resolve title: manual input overrides bibtex
        title = cleaned_data.get('title', '').strip()
        if title:
            cleaned_data['title'] = title
        elif bibtex_data.get('title'):
            cleaned_data['title'] = bibtex_data['title']
        else:
            self.add_error(None, 'Title: must provide a title directly or via BibTeX.')

        # resolve year: manual input overrides bibtex
        year = cleaned_data.get('year', '').strip()
        if year:
            cleaned_data['year'] = year
        elif bibtex_data.get('year'):
            cleaned_data['year'] = bibtex_data['year']
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
    """
    required_css_class = 'required'

    display_name = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=True,
        label='Display Name *',
    )

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
            del self.fields['author_name']
            del self.fields['publication_uuid']
            del self.fields['fabric_uuid']

    def clean(self):
        cleaned_data = super().clean()

        display_name = cleaned_data.get('display_name', '').strip()
        if display_name:
            cleaned_data['display_name'] = display_name

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
