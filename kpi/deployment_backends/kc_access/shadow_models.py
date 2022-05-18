# coding: utf-8
from datetime import date, datetime
from secrets import token_urlsafe
from typing import Optional

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.postgres.fields import JSONField as JSONBField
from django.core import checks
from django.core.exceptions import FieldDoesNotExist
from django.core.files.base import ContentFile
from django.core.signing import Signer
from django.db import (
    ProgrammingError,
    connections,
    models,
    router,
)
from django.utils import timezone
from django.utils.http import urlquote
from django_digest.models import PartialDigest

from kpi.constants import SHADOW_MODEL_APP_LABEL
from kpi.exceptions import (
    BadContentTypeException,
)
from kpi.mixins.audio_converter import ConverterMixin
from kpi.utils.hash import calculate_hash
from kpi.utils.datetime import one_minute_from_now
from .storage import (
    get_kobocat_storage,
    KobocatS3Boto3Storage,
)


def update_autofield_sequence(model):
    """
    Fixes the PostgreSQL sequence for the first (and only?) `AutoField` on
    `model`, à la `manage.py sqlsequencereset`
    """
    sql_template = (
        "SELECT setval(pg_get_serial_sequence('{table}','{column}'), "
        "coalesce(max({column}), 1), max({column}) IS NOT null) FROM {table};"
    )
    autofield = None
    for f in model._meta.get_fields():
        if isinstance(f, models.AutoField):
            autofield = f
            break
    if not autofield:
        return
    query = sql_template.format(
        table=model._meta.db_table, column=autofield.column
    )
    connection = connections[router.db_for_write(model)]
    with connection.cursor() as cursor:
        cursor.execute(query)


class ShadowModel(models.Model):
    """
    Allows identification of writeable and read-only shadow models
    """
    class Meta:
        managed = False
        abstract = True
        # TODO find out why it raises a warning when user logs in.
        # ```
        #   RuntimeWarning: Model '...' was already registered.
        #   Reloading models is not advised as it can lead to inconsistencies,
        #   most notably with related models
        # ```
        # Maybe because `SHADOW_MODEL_APP_LABEL` is not declared in
        # `INSTALLED_APP`
        # It's just used for `DefaultDatabaseRouter` conditions.
        app_label = SHADOW_MODEL_APP_LABEL

    @staticmethod
    def get_content_type_for_model(model):
        model_name_mapping = {
            'kobocatxform': ('logger', 'xform'),
            'readonlykobocatinstance': ('logger', 'instance'),
            'kobocatuserprofile': ('main', 'userprofile'),
            'kobocatuserobjectpermission': ('guardian', 'userobjectpermission'),
        }
        try:
            app_label, model_name = model_name_mapping[model._meta.model_name]
        except KeyError:
            raise NotImplementedError
        return KobocatContentType.objects.get(
            app_label=app_label, model=model_name)


class KobocatContentType(ShadowModel):
    """
    Minimal representation of Django 1.8's
    contrib.contenttypes.models.ContentType
    """
    app_label = models.CharField(max_length=100)
    model = models.CharField('python model class name', max_length=100)

    class Meta(ShadowModel.Meta):
        db_table = 'django_content_type'
        unique_together = (('app_label', 'model'),)

    def __str__(self):
        # Not as nice as the original, which returns a human-readable name
        # complete with whitespace. That requires access to the Python model
        # class, though
        return self.model


class KobocatDigestPartial(ShadowModel):

    user = models.ForeignKey('KobocatUser', on_delete=models.CASCADE)
    login = models.CharField(max_length=128, db_index=True)
    partial_digest = models.CharField(max_length=100)
    confirmed = models.BooleanField(default=True)

    class Meta(ShadowModel.Meta):
        db_table = "django_digest_partialdigest"

    @classmethod
    def sync(cls, user):
        """
        Mimics the behavior of `django_digest.models._store_partial_digests()`,
        but updates `KobocatDigestPartial` in the KoBoCAT database instead of
        `PartialDigest` in the KPI database
        """
        cls.objects.filter(user=user).delete()
        # Query for `user_id` since user PKs are synchronized
        for partial_digest in PartialDigest.objects.filter(user_id=user.pk):
            cls.objects.create(
                user=user,
                login=partial_digest.login,
                confirmed=partial_digest.confirmed,
                partial_digest=partial_digest.partial_digest,
            )


class KobocatGenericForeignKey(GenericForeignKey):

    def get_content_type(self, obj=None, id=None, using=None):
        if obj is not None:
            return KobocatContentType.objects.db_manager(obj._state.db).get_for_model(
                obj, for_concrete_model=self.for_concrete_model)
        elif id is not None:
            return KobocatContentType.objects.db_manager(using).get_for_id(id)
        else:
            # This should never happen. I love comments like this, don't you?
            raise Exception("Impossible arguments to GFK.get_content_type!")

    def get_forward_related_filter(self, obj):
        """See corresponding method on RelatedField"""
        return {
            self.fk_field: obj.pk,
            self.ct_field: KobocatContentType.objects.get_for_model(obj).pk,
        }

    def _check_content_type_field(self):
        try:
            field = self.model._meta.get_field(self.ct_field)
        except FieldDoesNotExist:
            return [
                checks.Error(
                    "The GenericForeignKey content type references the "
                    "nonexistent field '%s.%s'." % (
                        self.model._meta.object_name, self.ct_field
                    ),
                    obj=self,
                    id='contenttypes.E002',
                )
            ]
        else:
            if not isinstance(field, models.ForeignKey):
                return [
                    checks.Error(
                        "'%s.%s' is not a ForeignKey." % (
                            self.model._meta.object_name, self.ct_field
                        ),
                        hint=(
                            "GenericForeignKeys must use a ForeignKey to "
                            "'contenttypes.ContentType' as the 'content_type' field."
                        ),
                        obj=self,
                        id='contenttypes.E003',
                    )
                ]
            elif field.remote_field.model != KobocatContentType:
                return [
                    checks.Error(
                        "'%s.%s' is not a ForeignKey to 'contenttypes.ContentType'."
                        % (self.model._meta.object_name, self.ct_field),
                        hint=(
                            "GenericForeignKeys must use a ForeignKey to "
                            "'contenttypes.ContentType' as the 'content_type' field."
                        ),
                        obj=self,
                        id='contenttypes.E004',
                    )
                ]
            else:
                return []


class KobocatOneTimeAuthToken(ShadowModel):
    """
    One time authenticated token
    """
    HEADER = 'X-KOBOCAT-OTA-TOKEN'
    QS_PARAM = 'kc_ota_token'

    user = models.ForeignKey(
        'KobocatUser',
        related_name='authenticated_requests',
        on_delete=models.CASCADE,
    )
    token = models.CharField(max_length=50, default=token_urlsafe)
    expiration_time = models.DateTimeField()
    method = models.CharField(max_length=6)
    request_identifier = models.CharField(max_length=1000)

    class Meta(ShadowModel.Meta):
        db_table = 'api_onetimeauthtoken'
        unique_together = ('user', 'token', 'method')

    def get_header(self) -> dict:
        return {self.HEADER: self.token}

    @classmethod
    def get_or_create_token(
            cls,
            user: 'auth.User',
            method: str,
            request_identifier: str,
            use_identifier_as_token: bool = False,
            expiration_time: Optional[datetime] = None,
    ) -> 'KobocatOneTimeAuthToken':
        """
        Get or create an instance of KobocatOneTimeAuthToken and return it.

        If `use_identifier_as_token` is True, it generates the token based on
        the `request_identifier` instead of auto-generating it.
        """
        kc_user = KobocatUser.objects.get(id=user.pk)
        token_attrs = dict(
            user=kc_user, method=method, request_identifier=request_identifier,
            defaults={'expiration_time': expiration_time},
        )

        if use_identifier_as_token:
            # `Signer()` returns 'url:encoded-string'.
            # E.g: https://ee.kt.org/edit:a123bc'
            # We only want the last part.
            # When KoBoCAT tries to get the token, it reads the headers, then
            # the querystring parameters and finally uses the HTTP referrer if
            # none of the others worked. The headers and querystring parameters
            # cannot be transferred through Enketo Express, so we use its URL
            # to generate the token and let KoBoCAT compare it to its referrer.
            parts = Signer().sign(request_identifier).split(':')
            # TODO: consider removing Signer() as it's only value here is to
            # assure that the token will always be a consistent length (and,
            # for example, not overrun the size of the database column for long
            # URLs)
            token_attrs['token'] = parts[-1]

        auth_token, created = cls.objects.get_or_create(**token_attrs)
        if not created:
            # Make sure to reset the validity period of an existing token
            auth_token.expiration_time = expiration_time
            auth_token.save()

        return auth_token

    def save(self, *args, **kwargs):
        if not self.expiration_time:
            self.expiration_time = one_minute_from_now()

        super().save(*args, **kwargs)


class KobocatPermission(ShadowModel):
    """
    Minimal representation of Django 1.8's contrib.auth.models.Permission
    """
    name = models.CharField('name', max_length=255)
    content_type = models.ForeignKey(KobocatContentType, on_delete=models.CASCADE)
    codename = models.CharField('codename', max_length=100)

    class Meta(ShadowModel.Meta):
        db_table = 'auth_permission'
        unique_together = (('content_type', 'codename'),)
        ordering = ('content_type__app_label', 'content_type__model',
                    'codename')

    def __str__(self):
        return "%s | %s | %s" % (
            str(self.content_type.app_label),
            str(self.content_type),
            str(self.name))


class KobocatSubmissionCounter(ShadowModel):
    user = models.ForeignKey('shadow_model.KobocatUser', on_delete=models.CASCADE)
    count = models.IntegerField(default=0)
    timestamp = models.DateField()

    class Meta(ShadowModel.Meta):
        app_label = 'superuser_stats'
        db_table = 'logger_submissioncounter'
        verbose_name_plural = 'User Statistics'

    @classmethod
    def sync(cls, user):
        """
        Creates rows when the user is created so that the Admin UI doesn't freak
        out because it's looking for a row that doesn't exist
        """
        today = date.today()
        first = today.replace(day=1)

        queryset = cls.objects.filter(user_id=user.pk, timestamp=first)
        if not queryset.exists():
            # Todo: Handle race conditions
            cls.objects.create(user_id=user.pk, timestamp=first)

class KobocatUser(ShadowModel):

    username = models.CharField('username', max_length=30, unique=True)
    password = models.CharField('password', max_length=128)
    last_login = models.DateTimeField('last login', blank=True, null=True)
    is_superuser = models.BooleanField('superuser status', default=False)
    first_name = models.CharField('first name', max_length=30, blank=True)
    last_name = models.CharField('last name', max_length=150, blank=True)
    email = models.EmailField('email address', blank=True)
    is_staff = models.BooleanField('staff status', default=False)
    is_active = models.BooleanField('active', default=True)
    date_joined = models.DateTimeField('date joined', default=timezone.now)

    class Meta(ShadowModel.Meta):
        db_table = 'auth_user'

    @classmethod
    def sync(cls, auth_user):
        # NB: `KobocatUserObjectPermission` (and probably other things) depend
        # upon PKs being synchronized between KPI and KoBoCAT
        try:
            kc_auth_user = cls.objects.get(pk=auth_user.pk)
            assert kc_auth_user.username == auth_user.username
        except KobocatUser.DoesNotExist:
            kc_auth_user = cls(pk=auth_user.pk, username=auth_user.username)

        kc_auth_user.password = auth_user.password
        kc_auth_user.last_login = auth_user.last_login
        kc_auth_user.is_superuser = auth_user.is_superuser
        kc_auth_user.first_name = auth_user.first_name
        kc_auth_user.last_name = auth_user.last_name
        kc_auth_user.email = auth_user.email
        kc_auth_user.is_staff = auth_user.is_staff
        kc_auth_user.is_active = auth_user.is_active
        kc_auth_user.date_joined = auth_user.date_joined

        kc_auth_user.save()

        # We've manually set a primary key, so `last_value` in the sequence
        # `auth_user_id_seq` now lags behind `max(id)`. Fix it now!
        update_autofield_sequence(cls)

        # Update django-digest `PartialDigest`s in KoBoCAT.  This is only
        # necessary if the user's password has changed, but we do it always
        KobocatDigestPartial.sync(kc_auth_user)

        # Add the user to the table to prevent the errors in the admin page
        # and to ensure the user has a counter started for reporting
        KobocatSubmissionCounter.sync(kc_auth_user)


class KobocatUserObjectPermission(ShadowModel):
    """
    For the _sole purpose_ of letting us manipulate KoBoCAT
    permissions, this comprises the following django-guardian classes
    all condensed into one:

      * UserObjectPermission
      * UserObjectPermissionBase
      * BaseGenericObjectPermission
      * BaseObjectPermission

    CAVEAT LECTOR: The django-guardian custom manager,
    UserObjectPermissionManager, is NOT included!
    """
    permission = models.ForeignKey(KobocatPermission, on_delete=models.CASCADE)
    content_type = models.ForeignKey(KobocatContentType, on_delete=models.CASCADE)
    object_pk = models.CharField('object ID', max_length=255)
    content_object = KobocatGenericForeignKey(fk_field='object_pk')
    user = models.ForeignKey(KobocatUser, on_delete=models.CASCADE)

    class Meta(ShadowModel.Meta):
        db_table = 'guardian_userobjectpermission'
        unique_together = ['user', 'permission', 'object_pk']

    def __str__(self):
        # `unicode(self.content_object)` fails when the object's model
        # isn't known to this Django project. Let's use something more
        # benign instead.
        content_object_str = '{app_label}_{model} ({pk})'.format(
            app_label=self.content_type.app_label,
            model=self.content_type.model,
            pk=self.object_pk)
        return '%s | %s | %s' % (
            # unicode(self.content_object),
            content_object_str,
            str(getattr(self, 'user', False) or self.group),
            str(self.permission.codename))

    def save(self, *args, **kwargs):
        content_type = KobocatContentType.objects.get_for_model(
            self.content_object)
        if content_type != self.permission.content_type:
            raise BadContentTypeException(
                f"Cannot persist permission not designed for this "
                 "class (permission's type is {self.permission.content_type} "
                 "and object's type is {content_type}")
        return super().save(*args, **kwargs)


class KobocatUserPermission(ShadowModel):
    """ Needed to assign model-level KoBoCAT permissions """
    user = models.ForeignKey('KobocatUser', db_column='user_id',
                             on_delete=models.CASCADE)
    permission = models.ForeignKey('KobocatPermission',
                                   db_column='permission_id',
                                   on_delete=models.CASCADE)

    class Meta(ShadowModel.Meta):
        db_table = 'auth_user_user_permissions'


class KobocatUserProfile(ShadowModel):
    """
    From onadata/apps/main/models/user_profile.py
    Not read-only because we need write access to `require_auth`
    """
    class Meta(ShadowModel.Meta):
        db_table = 'main_userprofile'
        verbose_name = 'user profile'
        verbose_name_plural = 'user profiles'

    # This field is required.
    user = models.OneToOneField(KobocatUser,
                                related_name='profile',
                                on_delete=models.CASCADE)

    # Other fields here
    name = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=2, blank=True)
    organization = models.CharField(max_length=255, blank=True)
    home_page = models.CharField(max_length=255, blank=True)
    twitter = models.CharField(max_length=255, blank=True)
    description = models.CharField(max_length=255, blank=True)
    require_auth = models.BooleanField(
        default=False,
        verbose_name="Require authentication to see forms and submit data"
    )
    address = models.CharField(max_length=255, blank=True)
    phonenumber = models.CharField(max_length=30, blank=True)
    created_by = models.ForeignKey(KobocatUser, null=True, blank=True,
                                   on_delete=models.CASCADE)
    num_of_submissions = models.IntegerField(default=0)
    metadata = JSONBField(default=dict, blank=True)
    # We need to cast `is_active` to an (positive small) integer because KoBoCAT
    # is using `LazyBooleanField` which is an integer behind the scene.
    # We do not want to port this class to KPI only for one line of code.
    is_mfa_active = models.PositiveSmallIntegerField(default=False)

    @classmethod
    def set_mfa_status(cls, user_id: int, is_active: bool):

        try:
            user_profile, created = cls.objects.get_or_create(user_id=user_id)
        except cls.DoesNotExist:
            pass
        else:
            user_profile.is_mfa_active = int(is_active)
            user_profile.save(update_fields=['is_mfa_active'])


class KobocatToken(ShadowModel):

    key = models.CharField("Key", max_length=40, primary_key=True)
    user = models.OneToOneField(KobocatUser,
                                related_name='auth_token',
                                on_delete=models.CASCADE, verbose_name="User")
    created = models.DateTimeField("Created", auto_now_add=True)

    class Meta(ShadowModel.Meta):
        db_table = "authtoken_token"

    @classmethod
    def sync(cls, auth_token):
        try:
            # Token use a One-to-One relationship on User.
            # Thus, we can retrieve tokens from users' id. 
            kc_auth_token = cls.objects.get(user_id=auth_token.user_id)
        except KobocatToken.DoesNotExist:
            kc_auth_token = cls(pk=auth_token.pk, user_id=auth_token.user_id)

        kc_auth_token.save()


class KobocatXForm(ShadowModel):

    class Meta(ShadowModel.Meta):
        db_table = 'logger_xform'
        verbose_name = 'xform'
        verbose_name_plural = 'xforms'

    XFORM_TITLE_LENGTH = 255
    xls = models.FileField(null=True)
    xml = models.TextField()
    user = models.ForeignKey(KobocatUser, related_name='xforms', null=True,
                             on_delete=models.CASCADE)
    shared = models.BooleanField(default=False)
    shared_data = models.BooleanField(default=False)
    downloadable = models.BooleanField(default=True)
    id_string = models.SlugField()
    title = models.CharField(max_length=XFORM_TITLE_LENGTH)
    date_created = models.DateTimeField()
    date_modified = models.DateTimeField()
    uuid = models.CharField(max_length=32, default='')
    last_submission_time = models.DateTimeField(blank=True, null=True)
    num_of_submissions = models.IntegerField(default=0)
    kpi_asset_uid = models.CharField(max_length=32, null=True)

    @property
    def md5_hash(self):
        return calculate_hash(self.xml)

    @property
    def prefixed_hash(self):
        """
        Matches what's returned by the KC API
        """

        return "md5:%s" % self.md5_hash


class ReadOnlyModel(ShadowModel):

    read_only = True

    class Meta(ShadowModel.Meta):
        abstract = True


class ReadOnlyKobocatAttachment(ReadOnlyModel, ConverterMixin):

    class Meta(ReadOnlyModel.Meta):
        db_table = 'logger_attachment'

    instance = models.ForeignKey(
        'superuser_stats.ReadOnlyKobocatInstance',
        related_name='attachments',
        on_delete=models.CASCADE,
    )
    media_file = models.FileField(storage=get_kobocat_storage(), max_length=380,
                                  db_index=True)
    media_file_basename = models.CharField(
        max_length=260, null=True, blank=True, db_index=True)
    # `PositiveIntegerField` will only accommodate 2 GiB, so we should consider
    # `PositiveBigIntegerField` after upgrading to Django 3.1+
    media_file_size = models.PositiveIntegerField(blank=True, null=True)
    mimetype = models.CharField(
        max_length=100, null=False, blank=True, default=''
    )
    # TODO: hide attachments that were deleted or replaced; see
    # kobotoolbox/kobocat#792
    # replaced_at = models.DateTimeField(blank=True, null=True)

    @property
    def absolute_path(self):
        """
        Return the absolute path on local file system of the attachment.
        Otherwise, return the AWS url (e.g. https://...)
        """
        if not isinstance(get_kobocat_storage(), KobocatS3Boto3Storage):
            return self.media_file.path

        return self.media_file.url

    def get_or_create_conversion(self, filepath):
        """

        """
        kobocat_storage = get_kobocat_storage()

        if not kobocat_storage.exists(filepath):
            content = self.get_converter_content(self.format)
            kobocat_storage.save(filepath, ContentFile(content))

        if not isinstance(kobocat_storage, KobocatS3Boto3Storage):
            return filepath

        return kobocat_storage.url(self.mp3_storage_path)

    def protected_path(self, format_: Optional[str] = None):
        """
        Return path to be served as protected file served by NGINX
        """
        self.set_format(format_)

        if format_ in self.AVAILABLE_CONVERSIONS:
            attachment_file_path = f'{self.absolute_path}.{format_}'
            self.get_or_create_conversion(attachment_file_path)

        else:
            attachment_file_path = self.absolute_path

        if not isinstance(get_kobocat_storage(), KobocatS3Boto3Storage):
            # Django normally sanitizes accented characters in file names during
            # save on disk but some languages have extra letters
            # (out of ASCII character set) and must be encoded to let NGINX serve
            # them
            protected_url = urlquote(attachment_file_path.replace(
                settings.KOBOCAT_MEDIA_PATH, '/protected')
            )
        else:
            # Double-encode the S3 URL to take advantage of NGINX's
            # otherwise troublesome automatic decoding
            protected_url = f'/protected-s3/{urlquote(attachment_file_path)}'

        return protected_url

    @property
    def storage_path(self):
        return str(self.media_file)

    def set_format(self, file_format):
        self.format = file_format


class ReadOnlyKobocatInstance(ReadOnlyModel):

    class Meta(ReadOnlyModel.Meta):
        app_label = 'superuser_stats'
        db_table = 'logger_instance'
        verbose_name = 'Submissions by Country'
        verbose_name_plural = 'Submissions by Country'

    xml = models.TextField()
    user = models.ForeignKey(KobocatUser, null=True, on_delete=models.CASCADE)
    xform = models.ForeignKey(KobocatXForm, related_name='instances',
                              on_delete=models.CASCADE)
    date_created = models.DateTimeField()
    date_modified = models.DateTimeField()
    deleted_at = models.DateTimeField(null=True, default=None)
    status = models.CharField(max_length=20,
                              default='submitted_via_web')
    uuid = models.CharField(max_length=249, default='')


def safe_kc_read(func):
    def _wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ProgrammingError as e:
            raise ProgrammingError(
                'kc_access error accessing kobocat tables: {}'.format(str(e))
            )
    return _wrapper
