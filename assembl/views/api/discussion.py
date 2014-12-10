import json

from pyramid.httpexceptions import HTTPNotFound

from cornice import Service

from assembl.views.api import API_DISCUSSION_PREFIX

from assembl.models import Discussion

from ...auth import P_READ, P_ADMIN_DISC

discussion = Service(
    name='discussion',
    path=API_DISCUSSION_PREFIX,
    description="Manipulate a single Discussion object",
    renderer='json',
)


@discussion.get(permission=P_READ)
def get_discussion(request):
    discussion_id = request.matchdict['discussion_id']
    discussion = Discussion.get_instance(discussion_id)
    view_def = request.GET.get('view') or 'default'

    if not discussion:
        raise HTTPNotFound(
            "Discussion with id '%s' not found." % discussion_id)

    return discussion.generic_json(view_def)


# This should be a PUT, but the backbone save method is confused by
# discussion URLs.
@discussion.post(permission=P_ADMIN_DISC)
def post_discussion(request):
    discussion_id = request.matchdict['discussion_id']
    discussion = Discussion.get_instance(discussion_id)

    if not discussion:
        raise HTTPNotFound(
            "Discussion with id '%s' not found." % discussion_id)

    discussion_data = json.loads(request.body)

    discussion.topic = discussion_data.get('topic', discussion.slug)
    discussion.slug = discussion_data.get('slug', discussion.slug)
    discussion.objectives = discussion_data.get(
        'objectives', discussion.objectives)

    return {'ok': True}
