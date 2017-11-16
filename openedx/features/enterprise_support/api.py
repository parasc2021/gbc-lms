"""
APIs providing support for enterprise functionality.
"""
import logging
from functools import wraps

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.utils.http import urlencode
from django.utils.translation import ugettext as _
from edx_rest_api_client.client import EdxRestApiClient
from slumber.exceptions import HttpClientError, HttpNotFoundError, HttpServerError, SlumberBaseException

from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from openedx.core.lib.token_utils import JwtBuilder
from openedx.features.enterprise_support.utils import get_cache_key
from third_party_auth.pipeline import get as get_partial_pipeline
from third_party_auth.provider import Registry

try:
    from enterprise.models import EnterpriseCourseEnrollment, EnterpriseCustomer
except ImportError:
    pass


CONSENT_FAILED_PARAMETER = 'consent_failed'
LOGGER = logging.getLogger("edx.enterprise_helpers")


class EnterpriseApiException(Exception):
    """
    Exception for errors while communicating with the Enterprise service API.
    """
    pass


class ConsentApiClient(object):
    """
    Class for producing an Enterprise Consent service API client
    """

    def __init__(self, user):
        """
        Initialize an authenticated Consent service API client by using the
        provided user.
        """
        jwt = JwtBuilder(user).build_token([])
        url = configuration_helpers.get_value('ENTERPRISE_CONSENT_API_URL', settings.ENTERPRISE_CONSENT_API_URL)
        self.client = EdxRestApiClient(
            url,
            jwt=jwt,
            append_slash=False,
        )
        self.consent_endpoint = self.client.data_sharing_consent

    def revoke_consent(self, **kwargs):
        """
        Revoke consent from any existing records that have it at the given scope.

        This endpoint takes any given kwargs, which are understood as filtering the
        conceptual scope of the consent involved in the request.
        """
        return self.consent_endpoint.delete(**kwargs)

    def provide_consent(self, **kwargs):
        """
        Provide consent at the given scope.

        This endpoint takes any given kwargs, which are understood as filtering the
        conceptual scope of the consent involved in the request.
        """
        return self.consent_endpoint.post(kwargs)

    def consent_required(self, enrollment_exists=False, **kwargs):
        """
        Determine if consent is required at the given scope.

        This endpoint takes any given kwargs, which are understood as filtering the
        conceptual scope of the consent involved in the request.
        """

        # Call the endpoint with the given kwargs, and check the value that it provides.
        response = self.consent_endpoint.get(**kwargs)

        # No Enterprise record exists, but we're already enrolled in a course. So, go ahead and proceed.
        if enrollment_exists and not response.get('exists', False):
            return False

        # In all other cases, just trust the Consent API.
        return response['consent_required']


class EnterpriseServiceClientMixin(object):
    """
    Class for initializing an Enterprise API clients with service user.
    """

    def __init__(self):
        """
        Initialize an authenticated Enterprise API client by using the
        Enterprise worker user by default.
        """
        user = User.objects.get(username=settings.ENTERPRISE_SERVICE_WORKER_USERNAME)
        super(EnterpriseServiceClientMixin, self).__init__(user)


class ConsentApiServiceClient(EnterpriseServiceClientMixin, ConsentApiClient):
    """
    Class for producing an Enterprise Consent API client with service user.
    """
    pass


class EnterpriseApiClient(object):
    """
    Class for producing an Enterprise service API client.
    """

    def __init__(self, user):
        """
        Initialize an authenticated Enterprise service API client by using the
        provided user.
        """
        self.user = user
        jwt = JwtBuilder(user).build_token([])
        self.client = EdxRestApiClient(
            configuration_helpers.get_value('ENTERPRISE_API_URL', settings.ENTERPRISE_API_URL),
            jwt=jwt
        )

    def get_enterprise_customer(self, uuid):
        endpoint = getattr(self.client, 'enterprise-customer')
        return endpoint(uuid).get()

    def post_enterprise_course_enrollment(self, username, course_id, consent_granted):
        """
        Create an EnterpriseCourseEnrollment by using the corresponding serializer (for validation).
        """
        data = {
            'username': username,
            'course_id': course_id,
            'consent_granted': consent_granted,
        }
        endpoint = getattr(self.client, 'enterprise-course-enrollment')  # pylint: disable=literal-used-as-attribute
        try:
            endpoint.post(data=data)
        except (HttpClientError, HttpServerError):
            message = (
                "An error occured while posting EnterpriseCourseEnrollment for user {username} and "
                "course run {course_id} (consent_granted value: {consent_granted})"
            ).format(
                username=username,
                course_id=course_id,
                consent_granted=consent_granted,
            )
            LOGGER.exception(message)
            raise EnterpriseApiException(message)

    def fetch_enterprise_learner_data(self, site, user):
        """
        Fetch information related to enterprise from the Enterprise Service.

        Example:
            fetch_enterprise_learner_data(site, user)

        Argument:
            site: (Site) site instance
            user: (User) django auth user

        Returns:
            dict: {
                "enterprise_api_response_for_learner": {
                    "count": 1,
                    "num_pages": 1,
                    "current_page": 1,
                    "results": [
                        {
                            "enterprise_customer": {
                                "uuid": "cf246b88-d5f6-4908-a522-fc307e0b0c59",
                                "name": "TestShib",
                                "catalog": 2,
                                "active": true,
                                "site": {
                                    "domain": "example.com",
                                    "name": "example.com"
                                },
                                "enable_data_sharing_consent": true,
                                "enforce_data_sharing_consent": "at_login",
                                "branding_configuration": {
                                    "enterprise_customer": "cf246b88-d5f6-4908-a522-fc307e0b0c59",
                                    "logo": "https://open.edx.org/sites/all/themes/edx_open/logo.png"
                                },
                                "enterprise_customer_entitlements": [
                                    {
                                        "enterprise_customer": "cf246b88-d5f6-4908-a522-fc307e0b0c59",
                                        "entitlement_id": 69
                                    }
                                ]
                            },
                            "user_id": 5,
                            "user": {
                                "username": "staff",
                                "first_name": "",
                                "last_name": "",
                                "email": "staff@example.com",
                                "is_staff": true,
                                "is_active": true,
                                "date_joined": "2016-09-01T19:18:26.026495Z"
                            },
                            "data_sharing_consent_records": [
                                {
                                    "username": "staff",
                                    "enterprise_customer_uuid": "cf246b88-d5f6-4908-a522-fc307e0b0c59",
                                    "exists": true,
                                    "course_id": "course-v1:edX DemoX Demo_Course",
                                    "consent_provided": true,
                                    "consent_required": false
                                }
                            ]
                        }
                    ],
                    "next": null,
                    "start": 0,
                    "previous": null
                }
            }

        Raises:
            ConnectionError: requests exception "ConnectionError", raised if if ecommerce is unable to connect
                to enterprise api server.
            SlumberBaseException: base slumber exception "SlumberBaseException", raised if API response contains
                http error status like 4xx, 5xx etc.
            Timeout: requests exception "Timeout", raised if enterprise API is taking too long for returning
                a response. This exception is raised for both connection timeout and read timeout.

        """
        if not user.is_authenticated():
            return None

        api_resource_name = 'enterprise-learner'

        try:
            endpoint = getattr(self.client, api_resource_name)
            querystring = {'username': user.username}
            response = endpoint().get(**querystring)
        except (HttpClientError, HttpServerError):
            LOGGER.exception(
                'Failed to get enterprise-learner for user [%s] with client user [%s]',
                user.username,
                self.user.username
            )
            return None

        return response


class EnterpriseApiServiceClient(EnterpriseServiceClientMixin, EnterpriseApiClient):
    """
    Class for producing an Enterprise service API client with service user.
    """

    def get_enterprise_customer(self, uuid):
        """
        Fetch enterprise customer with enterprise service user and cache the
        API response`.
        """
        cache_key = get_cache_key(
            resource='enterprise-customer',
            resource_id=uuid,
            username=settings.ENTERPRISE_SERVICE_WORKER_USERNAME,
        )
        enterprise_customer = cache.get(cache_key)
        if not enterprise_customer:
            endpoint = getattr(self.client, 'enterprise-customer')
            enterprise_customer = endpoint(uuid).get()
            if enterprise_customer:
                cache.set(cache_key, enterprise_customer, settings.ENTERPRISE_API_CACHE_TIMEOUT)

        return enterprise_customer


def data_sharing_consent_required(view_func):
    """
    Decorator which makes a view method redirect to the Data Sharing Consent form if:

    * The wrapped method is passed request, course_id as the first two arguments.
    * Enterprise integration is enabled
    * Data sharing consent is required before accessing this course view.
    * The request.user has not yet given data sharing consent for this course.

    After granting consent, the user will be redirected back to the original request.path.

    """

    @wraps(view_func)
    def inner(request, course_id, *args, **kwargs):
        """
        Redirect to the consent page if the request.user must consent to data sharing before viewing course_id.

        Otherwise, just call the wrapped view function.
        """
        # Redirect to the consent URL, if consent is required.
        consent_url = get_enterprise_consent_url(request, course_id, enrollment_exists=True)
        if consent_url:
            real_user = getattr(request.user, 'real_user', request.user)
            LOGGER.warning(
                u'User %s cannot access the course %s because they have not granted consent',
                real_user,
                course_id,
            )
            return redirect(consent_url)

        # Otherwise, drop through to wrapped view
        return view_func(request, course_id, *args, **kwargs)

    return inner


def enterprise_enabled():
    """
    Determines whether the Enterprise app is installed
    """
    return 'enterprise' in settings.INSTALLED_APPS and settings.FEATURES.get('ENABLE_ENTERPRISE_INTEGRATION', False)


def enterprise_customer_uuid_for_request(request):
    """
    Check all the context clues of the request to gather a particular EnterpriseCustomer's UUID.
    """
    if not enterprise_enabled():
        return None

    enterprise_customer_uuid = None
    sso_provider_id = request.GET.get('tpa_hint')
    running_pipeline = get_partial_pipeline(request)
    if running_pipeline:
        # Determine if the user is in the middle of a third-party auth pipeline,
        # and set the sso_provider_id parameter to match if so.
        sso_provider_id = Registry.get_from_pipeline(running_pipeline).provider_id

    if sso_provider_id:
        # If we have a third-party auth provider, get the linked enterprise customer.
        try:
            # FIXME: Implement an Enterprise API endpoint where we can get the EC
            # directly via the linked SSO provider
            # Check if there's an Enterprise Customer such that the linked SSO provider
            # has an ID equal to the ID we got from the running pipeline or from the
            # request tpa_hint URL parameter.
            enterprise_customer_uuid = EnterpriseCustomer.objects.get(
                enterprise_customer_identity_provider__provider_id=sso_provider_id
            ).uuid
        except EnterpriseCustomer.DoesNotExist:
            pass
    else:
        # Check if we got an Enterprise UUID passed directly as either a query
        # parameter, or as a value in the Enterprise cookie.
        enterprise_customer_uuid = request.GET.get('enterprise_customer') or request.COOKIES.get(
            settings.ENTERPRISE_CUSTOMER_COOKIE_NAME
        )

    if not enterprise_customer_uuid and request.user.is_authenticated():
        # If there's no way to get an Enterprise UUID for the request, check to see
        # if there's already an Enterprise attached to the requesting user on the backend.
        learner_data = get_enterprise_learner_data(request.site, request.user)
        if learner_data:
            enterprise_customer_uuid = learner_data[0]['enterprise_customer']['uuid']

    return enterprise_customer_uuid


def enterprise_customer_for_request(request):
    """
    Check all the context clues of the request to determine if
    the request being made is tied to a particular EnterpriseCustomer.
    """
    enterprise_customer = None
    enterprise_customer_uuid = enterprise_customer_uuid_for_request(request)
    if enterprise_customer_uuid:
        # If we were able to obtain an EnterpriseCustomer UUID, go ahead
        # and use it to attempt to retrieve EnterpriseCustomer details
        # from the EnterpriseCustomer API.
        enterprise_api_client = EnterpriseApiServiceClient()
        if request.user.is_authenticated():
            enterprise_api_client = EnterpriseApiClient(user=request.user)

        try:
            enterprise_customer = enterprise_api_client.get_enterprise_customer(enterprise_customer_uuid)
        except HttpNotFoundError:
            enterprise_customer = None

    return enterprise_customer


def consent_needed_for_course(request, user, course_id, enrollment_exists=False):
    """
    Wrap the enterprise app check to determine if the user needs to grant
    data sharing permissions before accessing a course.
    """
    if not enterprise_enabled():
        return False

    consent_key = ('data_sharing_consent_needed', course_id)

    if request.session.get(consent_key) is False:
        return False

    enterprise_learner_details = get_enterprise_learner_data(request.site, user)
    if not enterprise_learner_details:
        consent_needed = False
    else:
        client = ConsentApiClient(user=request.user)
        consent_needed = any(
            client.consent_required(
                username=user.username,
                course_id=course_id,
                enterprise_customer_uuid=learner['enterprise_customer']['uuid'],
                enrollment_exists=enrollment_exists,
            )
            for learner in enterprise_learner_details
        )
    if not consent_needed:
        # Set an ephemeral item in the user's session to prevent us from needing
        # to make a Consent API request every time this function is called.
        request.session[consent_key] = False

    return consent_needed


def get_enterprise_consent_url(request, course_id, user=None, return_to=None, enrollment_exists=False):
    """
    Build a URL to redirect the user to the Enterprise app to provide data sharing
    consent for a specific course ID.

    Arguments:
    * request: Request object
    * course_id: Course key/identifier string.
    * user: user to check for consent. If None, uses request.user
    * return_to: url name label for the page to return to after consent is granted.
                 If None, return to request.path instead.
    """
    if not enterprise_enabled():
        return ''

    user = user or request.user

    if not consent_needed_for_course(request, user, course_id, enrollment_exists=enrollment_exists):
        return None

    if return_to is None:
        return_path = request.path
    else:
        return_path = reverse(return_to, args=(course_id,))

    url_params = {
        'enterprise_customer_uuid': enterprise_customer_uuid_for_request(request),
        'course_id': course_id,
        'next': request.build_absolute_uri(return_path),
        'failure_url': request.build_absolute_uri(
            reverse('dashboard') + '?' + urlencode(
                {
                    CONSENT_FAILED_PARAMETER: course_id
                }
            )
        ),
    }
    querystring = urlencode(url_params)
    full_url = reverse('grant_data_sharing_permissions') + '?' + querystring
    LOGGER.info('Redirecting to %s to complete data sharing consent', full_url)
    return full_url


def get_enterprise_learner_data(site, user):
    """
    Client API operation adapter/wrapper
    """
    if not enterprise_enabled():
        return None

    enterprise_learner_data = EnterpriseApiClient(user=user).fetch_enterprise_learner_data(site=site, user=user)
    if enterprise_learner_data:
        return enterprise_learner_data['results']


def get_dashboard_consent_notification(request, user, course_enrollments):
    """
    If relevant to the request at hand, create a banner on the dashboard indicating consent failed.

    Args:
        request: The WSGIRequest object produced by the user browsing to the Dashboard page.
        user: The logged-in user
        course_enrollments: A list of the courses to be rendered on the Dashboard page.

    Returns:
        str: Either an empty string, or a string containing the HTML code for the notification banner.
    """
    if not enterprise_enabled():
        return ''

    enrollment = None
    consent_needed = False
    course_id = request.GET.get(CONSENT_FAILED_PARAMETER)

    if course_id:

        enterprise_customer = enterprise_customer_for_request(request)
        if not enterprise_customer:
            return ''

        for course_enrollment in course_enrollments:
            if str(course_enrollment.course_id) == course_id:
                enrollment = course_enrollment
                break

        client = ConsentApiClient(user=request.user)
        consent_needed = client.consent_required(
            enterprise_customer_uuid=enterprise_customer['uuid'],
            username=user.username,
            course_id=course_id,
        )

    if consent_needed and enrollment:

        message_template = _(
            'If you have concerns about sharing your data, please contact your administrator '
            'at {enterprise_customer_name}.'
        )

        message = message_template.format(
            enterprise_customer_name=enterprise_customer['name'],
        )
        title = _(
            'Enrollment in {course_name} was not complete.'
        ).format(
            course_name=enrollment.course_overview.display_name,
        )

        return render_to_string(
            'enterprise_support/enterprise_consent_declined_notification.html',
            {
                'title': title,
                'message': message,
                'course_name': enrollment.course_overview.display_name,
            }
        )
    return ''


def insert_enterprise_pipeline_elements(pipeline):
    """
    If the enterprise app is enabled, insert additional elements into the
    pipeline related to enterprise.
    """
    if not enterprise_enabled():
        return

    additional_elements = (
        'enterprise.tpa_pipeline.handle_enterprise_logistration',
    )

    insert_point = pipeline.index('social_core.pipeline.social_auth.load_extra_data')
    for index, element in enumerate(additional_elements):
        pipeline.insert(insert_point + index, element)
