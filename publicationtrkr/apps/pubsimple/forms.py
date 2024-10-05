from django import forms
from django.forms import CheckboxSelectMultiple

from publicationtrkr.apps.pubsimple.models import PubSimple, ApiUser
from publicationtrkr.utils.fabric_auth import get_api_user


class PubSimpleForm(forms.ModelForm):
    """
    {
      "authors": [
        "string"
      ],
      "link": "string",
      "project_name": "string",
      "project_uuid": "string",
      "title": "string",
      "year": "string"
    }
    """
    required_css_class = 'required'

    title = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=True,
        label='Title',
    )

    link = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=False,
        label='Link',
    )

    year = forms.CharField(
        widget=forms.TextInput(attrs={'size': 60}),
        required=True,
        label='Year',
    )

    authors = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 1, 'cols': 20}),
        required=True,
        label='Authors (as comma separated list)',
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
        self.cleaned_data['authors'] = [a.strip() for a in str(self.cleaned_data['authors']).split(',')]

    class Meta:
        model = PubSimple
        fields = ['title', 'authors', 'link', 'year', 'project_name', 'project_uuid']
