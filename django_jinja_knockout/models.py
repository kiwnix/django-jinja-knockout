import datetime
import re

from django.core.exceptions import FieldDoesNotExist
from django.utils.dateparse import parse_date, parse_datetime
from django.apps import apps
from django.db import models
from django.db.models import Q
# Django>=1.8
from django.db.models.fields.related import ForeignObjectRel
# Django>=1.9
# from django.db.models.fields.reverse_related import ForeignObjectRel
from django.db.models.fields.related import ForeignObject
from django.contrib.auth.models import User, Permission
from django.contrib.contenttypes.fields import GenericForeignKey
from django.utils.translation import ugettext_lazy as _

from .admin import empty_value_display


# To be used as CHOICES argument value of NullBooleanField unique key.
MANY_NONE_SINGLE_TRUE = (
    (None, _('No')),
    (True, _('Yes')),
)


def normalize_fk_fieldname(fieldname):
    return fieldname[:-3] if fieldname.endswith('_id') else fieldname


def get_permission_object(permission_str):
    app_label, codename = permission_str.split('.')
    return Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()


def get_users_with_permission(permission_str, include_su=True):
    permission_obj = get_permission_object(permission_str)
    q = Q(groups__permissions=permission_obj) | Q(user_permissions=permission_obj)
    if include_su:
        q |= Q(is_superuser=True)
    return User.objects.filter(q).distinct()


def get_related_field_val(obj, fieldname, strict_related=True):
    curr_rel = obj
    fieldpath = fieldname.split('__') if isinstance(fieldname, str) else fieldname
    if strict_related:
        for _fieldname in fieldpath:
            curr_rel = getattr(curr_rel, _fieldname)
    else:
        for _fieldname in fieldpath:
            curr_rel = getattr(curr_rel, _fieldname, None)
            if curr_rel is None:
                return None
    return curr_rel


def get_related_field(obj, fieldname):
    if type(obj) is str:
        related_obj = apps.get_model(*obj.split('.'))
    else:
        related_obj = obj
    fieldpath = fieldname.split('__')
    if len(fieldpath) > 1:
        fieldname = fieldpath.pop()
        for _fieldname in fieldpath:
            curr_field = related_obj._meta.get_field(_fieldname)
            if hasattr(curr_field, 'related_model'):
                # Django>=1.9
                related_obj = curr_field.related_model
            else:
                # Django==1.8
                related_obj = curr_field.rel.to
    return related_obj._meta.get_field(fieldname)


def get_meta(obj, meta_attr, fieldname=None):
    if fieldname is None:
        return getattr(obj._meta, meta_attr)
    related_field = get_related_field(obj, fieldname)
    if isinstance(related_field, GenericForeignKey):
        return get_meta(obj, meta_attr, related_field.ct_field)
    else:
        return getattr(related_field, meta_attr)


def get_verbose_name(obj, fieldname=None):
    return get_meta(obj, 'verbose_name', fieldname)


def model_fields_meta(model, meta_attr):
    meta = {}
    for field in model._meta.get_fields():
        if isinstance(field, ForeignObjectRel):
            field_meta = getattr(field.related_model._meta, meta_attr)
        elif isinstance(field, GenericForeignKey):
            content_obj = getattr(model, field.ct_field)
            if content_obj is None:
                field_meta = ''
            else:
                if hasattr(content_obj, 'field'):
                    field_meta = getattr(content_obj.field, meta_attr)
                else:
                    field_meta = getattr(content_obj._meta, meta_attr)
        else:
            field_meta = getattr(field, meta_attr)
        meta[field.name] = field_meta
    return meta


def model_fields_verbose_names(model):
    if hasattr(model, 'get_fields_i18n'):
        return model.get_fields_i18n()
    else:
        return model_fields_meta(model, 'verbose_name')


# obj can be model class or an instance of model class.
# To iterate only some selected fields, specify fields list.
def yield_model_fieldnames(obj, fields=None):
    if fields is None:
        for field in obj._meta.fields:
            yield field.attname
    else:
        for fieldname in fields:
            yield fieldname


def yield_model_fields(obj, fields=None, skip_virtual=False):
    if fields is None:
        for field in obj._meta.fields:
            yield field.name, field
    else:
        for fieldname in fields:
            try:
                if '__' in fieldname:
                    yield fieldname, get_related_field(obj, fieldname)
                else:
                    yield fieldname, obj._meta.get_field(fieldname)
            except FieldDoesNotExist as e:
                if not skip_virtual:
                    raise e


def yield_related_models(obj, fields):
    for fieldname, field in yield_model_fields(obj, fields, skip_virtual=True):
        if isinstance(field, ForeignObject):
            yield fieldname, field.related_model


# Return dict of model fields name / value pairs like queryset values() for one model instance supplied.
def model_values(obj, fields=None, strict_related=True):
    row = {}
    for fieldname in yield_model_fieldnames(obj, fields):
        try:
            if '__' in fieldname:
                val = get_related_field_val(obj, fieldname, strict_related)
            else:
                val = getattr(obj, fieldname)
            if isinstance(val, models.Model):
                row[fieldname] = val.pk
            else:
                row[fieldname] = val
        except AttributeError:
            row[fieldname] = get_related_field_val(obj, fieldname)
    return row


def get_object_description(obj):
    if hasattr(obj, 'get_str_fields'):
        return obj.get_str_fields()
    else:
        return [str(obj)]


# Get selected choice str from the list of defined choices for Django model field choices.
def get_choice_str(choices, selected_choice):
    for choice, choice_str in choices:
        if choice == selected_choice:
            return choice_str
    return None


# Check whether actual file of FileField exists (is not deleted / moved out).
def file_exists(obj):
    return obj.storage.exists(obj.name)


def wakeup_user(user):
    if hasattr(user, '_wrapped') and hasattr(user, '_setup'):
        if user._wrapped.__class__ == object:
            user._setup()
        user = user._wrapped
    return user


class NestedSerializer:

    scalar_display = {
        None: empty_value_display,
        False: _('No'),
        True: _('Yes'),
    }

    def __init__(self, obj=None, metadata=None):
        self.treepath = []
        self.obj = obj
        self.metadata = {} if metadata is None else metadata
        self.is_anon = None

    def to_dict(self, nesting_level=1):
        return self._to_dict(self.obj, nesting_level)

    def get_str_type(self, field):
        if field is None:
            return None
        else:
            path = str(type(field))
            mtch = re.search(r"<\w*?\s*?'(.*?)'>", path)
            if mtch is None:
                return path
            else:
                grp = mtch.group(1).split('.')
                return grp[-1]

    def field_to_dict(self, obj, field_name, verbose_name, nesting_level, str_fields):
        result = None, False
        field = obj._meta.get_field(field_name)
        if not isinstance(field, (models.AutoField, models.ManyToOneRel)):
            choices_fn = 'get_{}_display'.format(field_name)
            if hasattr(obj, choices_fn):
                v = getattr(obj, choices_fn)()
            else:
                try:
                    v = getattr(obj, field_name)
                except AttributeError:
                    return result
            self.treepath.append(field_name)
            metadata = {
                'verbose_name': str(verbose_name),
                'is_anon': field_name in str_fields,
                'type': self.get_str_type(field),
            }
            if isinstance(v, models.Model):
                result = self._to_dict(v, nesting_level - 1), True
            elif isinstance(v, (datetime.date, datetime.datetime)):
                result = v.isoformat(), True
            elif isinstance(v, (int, float, type(None), bool, str)):
                result = v, True
            elif field_name in str_fields:
                result = str_fields[field_name], True
                metadata['type'] = self.get_str_type(v)
            else:
                # Not a valid JSON type
                result = str(v), True
            self.metadata['›'.join(self.treepath)] = metadata
            self.treepath.pop()
        else:
            if field_name in str_fields:
                self.metadata['›'.join(self.treepath)] = {
                    'verbose_name': str(verbose_name),
                    'is_anon': True,
                    'type': self.get_str_type(str_fields[field_name]),
                }
                result = str_fields[field_name], True
        return result

    def _to_dict(self, obj, nesting_level):
        model_dict = {}
        if nesting_level > 0:
            if hasattr(obj, 'get_str_fields'):
                str_fields = obj.get_str_fields()
            else:
                str_fields = {}
            for field_name, verbose_name in model_fields_meta(obj, 'verbose_name').items():
                val, exists = self.field_to_dict(obj, field_name, verbose_name, nesting_level, str_fields)
                if exists:
                    model_dict[field_name] = val
            return model_dict
        elif hasattr(obj, 'get_str_fields'):
            verbose_names = model_fields_meta(obj, 'verbose_name')
            for field_name, val in obj.get_str_fields().items():
                field = getattr(obj, field_name, None)
                if field_name in verbose_names:
                    self.treepath.append(field_name)
                    self.metadata['›'.join(self.treepath)] = {
                        'verbose_name': str(verbose_names[field_name]),
                        'is_anon': True,
                        'type': self.get_str_type(field),
                    }
                    model_dict[field_name] = val
                    self.treepath.pop()
            return model_dict if len(model_dict) > 0 else str(obj)
        else:
            return str(obj)

    def localize_model_dict(self, model_dict, is_anon=None):
        self.is_anon = is_anon
        if self.metadata is None:
            return model_dict
        else:
            return self._localize_model_dict(model_dict, {})

    def _localize_model_dict(self, model_dict, local_model_dict):
        for k, v in model_dict.items():
            self.treepath.append(k)
            treepath_str = '›'.join(self.treepath)
            metadata = self.metadata[treepath_str] if treepath_str in self.metadata else {
                'verbose_name': k,
                'is_anon': False,
                'type': None,
            }
            if self.is_anon is None or self.is_anon == metadata['is_anon']:
                local_key = metadata['verbose_name']
                if isinstance(v, dict):
                    local_model_dict[local_key] = self._localize_model_dict(
                        v, {}
                    )
                else:
                    if v in self.scalar_display:
                        local_model_dict[local_key] = self.scalar_display[v]
                    elif metadata['type'] == 'DateTimeField':
                        local_model_dict[local_key] = format_local_date(parse_datetime(v))
                    elif metadata['type'] == 'DateField':
                        local_model_dict[local_key] = format_local_date(parse_date(v))
                    else:
                        local_model_dict[local_key] = v
            self.treepath.pop()
        return local_model_dict


from .tpl import format_local_date  # noqa
