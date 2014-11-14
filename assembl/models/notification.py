# coding=UTF-8
from datetime import datetime
from collections import defaultdict
from abc import abstractmethod
from time import sleep
import transaction
import os
import email
from sqlalchemy import (
    Column,
    Boolean,
    Integer,
    String,
    Float,
    UnicodeText,
    DateTime,
    ForeignKey,
    event,
    inspect
)
from sqlalchemy.orm import (
    relationship, backref, aliased, contains_eager, joinedload)
from zope import interface
from pyramid.httpexceptions import HTTPUnauthorized, HTTPBadRequest

from . import  Base, DiscussionBoundBase
from ..lib.model_watcher import IModelEventWatcher
from ..lib.decl_enums import DeclEnum
from ..lib.frontend_urls import FrontendUrls
from .auth import (User, Everyone, P_ADMIN_DISC)
from .discussion import Discussion
from .post import Post, SynthesisPost
from jinja2 import Environment, PackageLoader
from email import (charset as Charset)
from email.mime.text import MIMEText
from gettext import gettext, ngettext
_ = gettext

jinja_env = Environment(loader=PackageLoader('assembl', 'templates'), extensions=['jinja2.ext.i18n'])
jinja_env.install_gettext_callables(gettext, ngettext, newstyle=True)

# Don't BASE64-encode UTF-8 messages so that we avoid unwanted attention from
# some spam filters.
utf8_charset = Charset.Charset('utf-8')
utf8_charset.body_encoding = None # Python defaults to BASE64

class SafeMIMEText(MIMEText):
    def __init__(self, text, subtype, charset):
        self.encoding = charset
        if charset == 'utf-8':
            # Unfortunately, Python < 3.5 doesn't support setting a Charset instance
            # as MIMEText init parameter (http://bugs.python.org/issue16324).
            # We do it manually and trigger re-encoding of the payload.
            MIMEText.__init__(self, text, subtype, None)
            del self['Content-Transfer-Encoding']
            self.set_payload(text, utf8_charset)
            self.replace_header('Content-Type', 'text/%s; charset="%s"' % (subtype, charset))
        else:
            MIMEText.__init__(self, text, subtype, charset)

class NotificationSubscriptionClasses(DeclEnum):
    #System notifications (can't unsubscribe)
    EMAIL_BOUNCED = "EMAIL_BOUNCED", "Mandatory"
    EMAIL_VALIDATE = "EMAIL_VALIDATE", "Mandatory"
    RECOVER_ACCOUNT = "RECOVER_ACCOUNT", ""
    RECOVER_PASSWORD = "RECOVER_PASSWORD", ""
    PARTICIPATED_FOR_FIRST_TIME_WELCOME = "PARTICIPATED_FOR_FIRST_TIME_WELCOME", "Mandatory"
    SUBSCRIPTION_WELCOME = "SUBSCRIPTION_WELCOME", "Mandatory"

    # Core notification (unsubscribe strongly discuraged)
    FOLLOW_SYNTHESES = "FOLLOW_SYNTHESES", ""
    FOLLOW_OWN_MESSAGES_DIRECT_REPLIES = "FOLLOW_OWN_MESSAGES_DIRECT_REPLIES", "Mandatory?"
    # Note:  indirect replies are FOLLOW_THREAD_DISCUSSION
    SESSIONS_STARTING = "SESSIONS_STARTING", ""
    #Follow phase changes?
    FOLLOW_IDEA_FAMILY_DISCUSSION = "FOLLOW_IDEA_FAMILY_DISCUSSION", ""
    FOLLOW_IDEA_FAMILY_MEMBERSHIP_CHANGES = "FOLLOW_IDEA_FAMILY_MEMBERSHIP_CHANGES", ""
    FOLLOW_IDEA_FAMILY_SUB_IDEA_SUGGESTIONS = "FOLLOW_IDEA_FAMILY_SUB_IDEA_SUGGESTIONS", ""
    FOLLOW_IDEA_CANNONICAL_EXPRESSIONS_CHANGED = "FOLLOW_IDEA_CANNONICAL_EXPRESSIONS_CHANGED", "Title or description changed"
    FOLLOW_OWN_MESSAGES_NUGGETS = "FOLLOW_OWN_MESSAGES_NUGGETS", ""
    FOLLOW_ALL_MESSAGES = "FOLLOW_ALL_MESSAGES", "NOT the same as following root idea"
    FOLLOW_ALL_THREAD_NEWLY_PARTICIPATED_IN = "FOLLOW_ALL_THREAD_NEWLY_PARTICIPATED_IN", "Pseudo-notification, that will create new FOLLOW_THREAD_DISCUSSION notifications (so one can unsubscribe)"
    FOLLOW_THREAD_DISCUSSION = "FOLLOW_THREAD_DISCUSSION", ""
    FOLLOW_USER_POSTS = "FOLLOW_USER_POSTS", ""

    #System error notifications
    SYSTEM_ERRORS = "SYSTEM_ERRORS", ""
    # Abstract notification types. Those need not be in the constraint, so no migration.
    ABSTRACT_NOTIFICATION_SUBSCRIPTION = "ABSTRACT_NOTIFICATION_SUBSCRIPTION"
    ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_DISCUSSION = "ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_DISCUSSION"
    ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_OBJECT = "ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_OBJECT"
    ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_POST = "ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_POST"
    ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_IDEA = "ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_IDEA"
    ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_EXTRACT = "ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_EXTRACT"
    ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_USERACCOUNT = "ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_USERACCOUNT"


class NotificationCreationOrigin(DeclEnum):
    USER_REQUESTED = "USER_REQUESTED", "A direct user action created the notification subscription"
    DISCUSSION_DEFAULT = "DISCUSSION_DEFAULT", "The notification subscription was created by the default discussion configuration"
    PARENT_NOTIFICATION = "PARENT_NOTIFICATION", "The notification subscription was created by another subscription (such as following all message threads a user participated in"

class NotificationSubscriptionStatus(DeclEnum):
    ACTIVE = "ACTIVE", "Normal status, subscription will create notifications"
    UNSUBSCRIBED = "UNSUBSCRIBED", "The user explicitely unsubscribed from this notification"
    INACTIVE_DFT = "INACTIVE_DFT", "This subscription is defined in the template, but not subscribed by default."

class NotificationSubscription(DiscussionBoundBase):
    """
    a subscription to a specific type of notification. Subclasses will implement the actual code
    """
    __tablename__ = "notification_subscription"
    id = Column(
        Integer,
        primary_key=True)
    type = Column(
        NotificationSubscriptionClasses.db_type(),
        nullable=False,
        index=True)
    discussion_id = Column(
        Integer,
        ForeignKey('discussion.id',
                   ondelete='CASCADE',
                   onupdate='CASCADE'),
        nullable=False,
        index=True,
    )
    discussion = relationship(
        Discussion,
        backref=backref('notificationSubscriptions',
                        cascade="all, delete-orphan")
    )
    creation_date = Column(
        DateTime,
        nullable = False,
        default = datetime.utcnow)
    creation_origin = Column(
        NotificationCreationOrigin.db_type(),
        nullable = False)
    parent_subscription_id = Column(
        Integer,
        ForeignKey(
            'notification_subscription.id',
            ondelete='CASCADE',
            onupdate='CASCADE'),
        nullable = True)
    children_subscriptions = relationship(
        "NotificationSubscription",
        foreign_keys=[parent_subscription_id],
        backref=backref('parent_subscription', remote_side=[id]),
    )
    status = Column(
        NotificationSubscriptionStatus.db_type(),
        nullable = False,
        index = True,
        default = NotificationSubscriptionStatus.ACTIVE)
    last_status_change_date = Column(
        DateTime,
        nullable = False,
        default = datetime.utcnow)
    user_id = Column(
        Integer,
        ForeignKey(
            'user.id',
            ondelete='CASCADE',
            onupdate='CASCADE'),
            nullable = False,
            index = True)
    user = relationship(
        User,
        backref=backref(
            'notification_subscriptions', order_by=creation_date,
            cascade="all, delete-orphan")
    )

    #allowed_transports Ex: email_bounce cannot be bounced by the same email.  For now we'll special case in code
    priority = 1 #An integer, if more than one subsciption match for one event, only the one with the lowest integer can create a notification
    unsubscribe_allowed = False

    __mapper_args__ = {
        'polymorphic_identity': NotificationSubscriptionClasses.ABSTRACT_NOTIFICATION_SUBSCRIPTION,
        'polymorphic_on': 'type',
        'with_polymorphic': '*'
    }

    def get_discussion_id(self):
        return self.discussion_id

    def class_description(self):
        return self.type.description

    @abstractmethod
    def followed_object(self):
        pass

    @classmethod
    def get_discussion_condition(cls, discussion_id):
        return cls.discussion_id == discussion_id

    @abstractmethod
    def wouldCreateNotification(self, discussion_id, verb, object):
        return False

    @classmethod
    def findApplicableInstances(cls, discussion_id, verb, object, user=None):
        """
        Returns all subscriptions that would fire on the object, and verb given

        This naive implementation instanciates every ACTIVE subscription for every user,
        and calls "would fire" for each.  It is expected that most subclasses will
        override this with a more optimal implementation
        """
        applicable_subscriptions = []
        subscriptionsQuery = cls.db.query(cls)
        subscriptionsQuery = subscriptionsQuery.filter(cls.status==NotificationSubscriptionStatus.ACTIVE);
        subscriptionsQuery = subscriptionsQuery.filter(cls.discussion_id==discussion_id);
        if user:
            subscriptionsQuery.filter(cls.user==user)

        for subscription in subscriptionsQuery:
            if(subscription.wouldCreateNotification(object.get_discussion_id(), verb, object)):
                applicable_subscriptions.append(subscription)
        return applicable_subscriptions

    @abstractmethod
    def process(self, discussion_id, verb, objectInstance, otherApplicableSubscriptions):
        pass

    def get_human_readable_description(self):
        """ A human readable description of this notification subscription
        Default implementation, expected to be overriden by child classes """
        return self.external_typename()

    def update_json(self, json, user_id=Everyone):
        from ..auth.util import user_has_permission
        if self.user_id:
            if user_id != self.user_id:
                if not user_has_permission(self.discussion_id, user_id, P_ADMIN_DISC):
                    raise HTTPUnauthorized()
            # For now, do not allow changing user, it's way too complicated.
            if 'user' in json and User.get_database_id(json['user']) != self.user_id:
                raise HTTPBadRequest()
        else:
            json_user_id = json.get('user', None)
            if json_user_id is None:
                json_user_id = user_id
            else:
                json_user_id = User.get_database_id(json_user_id)
                if json_user_id != user_id and not user_has_permission(self.discussion_id, user_id, P_ADMIN_DISC):
                    raise HTTPUnauthorized()
            self.user_id = json_user_id
        if self.discussion_id:
            if 'discussion_id' in json and Discussion.get_database_id(json['discussion_id']) != self.discussion_id:
                raise HTTPBadRequest()
        else:
            discussion_id = json.get('discussion', None)
            if discussion_id is None:
                raise HTTPBadRequest()
            self.discussion_id = Discussion.get_database_id(discussion_id)
        new_type = json.get('@type', self.type)
        if self.external_typename() != new_type:
            polymap = inspect(self.__class__).polymorphic_identity
            if new_type not in polymap:
                raise HTTPBadRequest()
            new_type = polymap[new_type].class_
            new_instance = self.change_class(new_type)
            return new_instance.update_json(json)
        creation_origin = json.get('creation_origin', None)
        if creation_origin is not None:
            self.creation_origin = NotificationCreationOrigin.from_string(creation_origin)
        if json.get('parent_subscription', None) is not None:
            self.parent_subscription_id = self.get_database_id(json['parent_subscription'])
        status = json.get('status', None)
        if status:
            status = NotificationSubscriptionStatus.from_string(status)
            if status != self.status:
                self.status = status
                self.last_status_change_date = datetime.now()
        return self


@event.listens_for(NotificationSubscription.status, 'set', propagate=True)
def update_last_status_change_date(target, value, oldvalue, initiator):
    target.last_status_change_date = datetime.utcnow()


class NotificationSubscriptionGlobal(NotificationSubscription):
    __mapper_args__ = {
        'polymorphic_identity': NotificationSubscriptionClasses.ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_DISCUSSION
    }

    def followed_object(self):
        pass


class NotificationSubscriptionOnObject(NotificationSubscription):
    __mapper_args__ = {
        'polymorphic_identity': NotificationSubscriptionClasses.ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_OBJECT
    }

    def followed_object(self):
        pass

class NotificationSubscriptionOnPost(NotificationSubscriptionOnObject):

    __tablename__ = "notification_subscription_on_post"
    __mapper_args__ = {
        'polymorphic_identity': NotificationSubscriptionClasses.ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_POST
    }

    id = Column(Integer, ForeignKey(
        NotificationSubscription.id,
        ondelete='CASCADE',
        onupdate='CASCADE'
    ), primary_key=True)

    post_id = Column(
        Integer, ForeignKey("post.id",
            ondelete='CASCADE', onupdate='CASCADE'))

    post = relationship("Post", backref=backref(
        "subscriptions_on_post", cascade="all, delete-orphan"))

    def followed_object(self):
        return self.post

    def update_json(self, json, user_id=Everyone):
        updated = super(NotificationSubscriptionOnPost, self).update_json(json, user_id)
        if updated == self:
            self.post_id = json.get('post_id', self.post_id)
        return updated


class NotificationSubscriptionOnIdea(NotificationSubscriptionOnObject):

    __tablename__ = "notification_subscription_on_idea"
    __mapper_args__ = {
        'polymorphic_identity': NotificationSubscriptionClasses.ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_IDEA
    }

    id = Column(Integer, ForeignKey(
        NotificationSubscription.id,
        ondelete='CASCADE',
        onupdate='CASCADE'
    ), primary_key=True)

    idea_id = Column(
        Integer, ForeignKey("idea.id",
            ondelete='CASCADE', onupdate='CASCADE'))

    idea = relationship("Idea", backref=backref(
        "subscriptions_on_idea", cascade="all, delete-orphan"))

    def followed_object(self):
        return self.idea

    def update_json(self, json, user_id=Everyone):
        updated = super(NotificationSubscriptionOnPost, self).update_json(json, user_id)
        if updated == self:
            self.idea_id = json.get('idea_id', self.idea_id)
        return updated


class NotificationSubscriptionOnExtract(NotificationSubscriptionOnObject):

    __tablename__ = "notification_subscription_on_extract"
    __mapper_args__ = {
        'polymorphic_identity': NotificationSubscriptionClasses.ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_EXTRACT
    }

    id = Column(Integer, ForeignKey(
        NotificationSubscription.id,
        ondelete='CASCADE',
        onupdate='CASCADE'
    ), primary_key=True)

    extract_id = Column(
        Integer, ForeignKey("extract.id",
            ondelete='CASCADE', onupdate='CASCADE'))

    extract = relationship("Extract", backref=backref(
        "subscriptions_on_extract", cascade="all, delete-orphan"))

    def followed_object(self):
        return self.extract

    def update_json(self, json, user_id=Everyone):
        updated = super(NotificationSubscriptionOnPost, self).update_json(json, user_id)
        if updated == self:
            self.extract_id = json.get('extract_id', self.extract_id)
        return updated


class NotificationSubscriptionOnUserAccount(NotificationSubscriptionOnObject):

    __tablename__ = "notification_subscription_on_useraccount"
    __mapper_args__ = {
        'polymorphic_identity': NotificationSubscriptionClasses.ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_USERACCOUNT
    }

    id = Column(Integer, ForeignKey(
        NotificationSubscription.id,
        ondelete='CASCADE',
        onupdate='CASCADE'
    ), primary_key=True)

    on_user_id = Column(
        Integer, ForeignKey("user.id",
            ondelete='CASCADE', onupdate='CASCADE'))

    on_user = relationship("User", foreign_keys=[on_user_id], backref=backref(
        "subscriptions_on_user", cascade="all, delete-orphan"))

    def followed_object(self):
        return self.user

    def update_json(self, json, user_id=Everyone):
        updated = super(NotificationSubscriptionOnPost, self).update_json(json, user_id)
        if updated == self:
            self.on_user_id = json.get('on_user_id', self.on_user_id)
        return updated


class CrudVerbs():
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class NotificationSubscriptionFollowSyntheses(NotificationSubscriptionGlobal):
    priority = 1
    unsubscribe_allowed = True

    def get_human_readable_description(self):
        return gettext("A periodic synthesis of the discussion is posted by the moderator")

    def wouldCreateNotification(self, discussion_id, verb, object):
        return (verb == CrudVerbs.CREATE) and isinstance(object, SynthesisPost) and discussion_id == object.get_discussion_id()

    def process(self, discussion_id, verb, objectInstance, otherApplicableSubscriptions):
        from ..tasks.notify import notify
        assert self.wouldCreateNotification(discussion_id, verb, objectInstance)
        notification = NotificationOnPostCreated(
            post = objectInstance,
            first_matching_subscription = self,
            push_method = NotificationPushMethodType.EMAIL,
            #push_address = TODO
            )
        self.db.add(notification)
        self.db.commit()
        notify.delay(notification.id)

    __mapper_args__ = {
        'polymorphic_identity': NotificationSubscriptionClasses.FOLLOW_SYNTHESES
    }

class NotificationSubscriptionFollowAllMessages(NotificationSubscriptionGlobal):
    priority = 1
    unsubscribe_allowed = True

    def get_human_readable_description(self):
        return _("Any message is posted to the discussion")
    
    def wouldCreateNotification(self, discussion_id, verb, object):
        return (verb == CrudVerbs.CREATE) and isinstance(object, Post) and discussion_id == object.get_discussion_id()

    def process(self, discussion_id, verb, objectInstance, otherApplicableSubscriptions):
        assert self.wouldCreateNotification(discussion_id, verb, objectInstance)
        from sqlalchemy import inspect
        from ..tasks.notify import notify
        notification = NotificationOnPostCreated(
            post_id = objectInstance.id,
            first_matching_subscription = self,
            push_method = NotificationPushMethodType.EMAIL,
            #push_address = TODO
            )
        self.db.add(notification)
        self.db.commit()
        notify.delay(notification.id)

    __mapper_args__ = {
        'polymorphic_identity': NotificationSubscriptionClasses.FOLLOW_ALL_MESSAGES
    }

class NotificationSubscriptionFollowOwnMessageDirectReplies(NotificationSubscriptionGlobal):
    priority = 1
    unsubscribe_allowed = True

    def get_human_readable_description(self):
        return _("Someone directly responds to any message you posted")
    
    def wouldCreateNotification(self, discussion_id, verb, object):
        return ( (verb == CrudVerbs.CREATE)
                 and isinstance(object, Post)
                 and discussion_id == object.get_discussion_id()
                 and object.parent is not None
                 and object.parent.creator == self.user
                 )

    def process(self, discussion_id, verb, objectInstance, otherApplicableSubscriptions):
        assert self.wouldCreateNotification(discussion_id, verb, objectInstance)
        from ..tasks.notify import notify
        notification = NotificationOnPostCreated(
            post = objectInstance,
            first_matching_subscription = self,
            push_method = NotificationPushMethodType.EMAIL,
            #push_address = TODO
            )
        self.db.add(notification)
        self.db.commit()
        notify.delay(notification.id)

    __mapper_args__ = {
        'polymorphic_identity': NotificationSubscriptionClasses.FOLLOW_OWN_MESSAGES_DIRECT_REPLIES
    }


def waiting_get(objectClass, objectId):
    # Waiting for an object to be committed on another thread
    for i in range(100):
        objectInstance = objectClass.get(objectId)
        if objectInstance is not None:
            return objectInstance
        sleep(0.02)


class ModelEventWatcherNotificationSubscriptionDispatcher(object):
    interface.implements(IModelEventWatcher)

    def processEvent(self, verb, objectClass, objectId):
        from ..lib.utils import get_concrete_subclasses_recursive
        assert objectId
        objectInstance = waiting_get(objectClass, objectId)
        assert objectInstance
        assert objectInstance.id
        #We need the discussion id
        assert isinstance(objectInstance, DiscussionBoundBase)
        applicableInstancesByUser = defaultdict(list)
        subscriptionClasses = get_concrete_subclasses_recursive(NotificationSubscription)
        for subscriptionClass in subscriptionClasses:
            applicableInstances = subscriptionClass.findApplicableInstances(objectInstance.get_discussion_id(), CrudVerbs.CREATE, objectInstance)
            for subscription in applicableInstances:
                applicableInstancesByUser[subscription.user_id].append(subscription)
        with transaction.manager:
            for userId, applicableInstances in applicableInstancesByUser.iteritems():
                if(len(applicableInstances) > 0):
                    applicableInstances.sort(cmp=lambda x,y: cmp(x.priority, y.priority))
                    applicableInstances[0].process(objectInstance.get_discussion_id(), verb, objectInstance, applicableInstances[1:])


    def processPostCreated(self, id):
        print "processPostCreated", id
        self.processEvent(CrudVerbs.CREATE, Post, id)

    def processIdeaCreated(self, id):
        print "processIdeaCreated", id

    def processIdeaModified(self, id, version):
        print "processIdeaModified", id, version

    def processIdeaDeleted(self, id):
        print "processIdeaDeleted", id

    def processExtractCreated(self, id):
        print "processExtractCreated", id

    def processExtractModified(self, id, version):
        print "processExtractModified", id, version

    def processExtractDeleted(self, id):
        print "processExtractDeleted", id

    def processAccountCreated(self, id):
        print "processAccountCreated", id

    def processAccountModified(self, id):
        print "processAccountModified", id

class NotificationPushMethodType(DeclEnum):
    """
    The type of event that caused the notification to be created
    """
    EMAIL = "EMAIL", "Email notification"
    LOGIN_NOTIFICATION = "LOGIN_NOTIFICATION", "A notification upon next login to Assembl"

class NotificationDeliveryStateType(DeclEnum):
    """
    The delivery state of the notification.  Essentially it's licefycle
    """
    QUEUED = "QUEUED", "Active notification ready to be sent over some transport"
    DELIVERY_IN_PROGRESS = "DELIVERY_IN_PROGRESS", "Active notification that has successfully been handed over some transport, but whose reception hasn't been confirmed"
    DELIVERY_CONFIRMED = "DELIVERY_CONFIRMED", "Active notification whose delivery has been confirmed by the transport"
    READ_CONFIRMED = "READ_CONFIRMED", "Active notification that the user has unambiguously received (ex:  clicked on a link in the notification)"
    DELIVERY_FAILURE = "DELIVERY_FAILURE", "Inactive notification whose failure has been confirmed by the transport.  If possible should be retried on another channel"
    DELIVERY_TEMPORARY_FAILURE = "DELIVERY_TEMPORARY_FAILURE", "Active notification whose delivery is delayed.  Ex:  email soft-bounce, smtp server is down, etc."
    OBSOLETED = "OBSOLETED", "Inactive notification that has been rendered useless by some event.  For example, the user has read the message the notification was about from the web interface before the notification was delivered"
    EXPIRED = "EXPIRED", "Similar to OBSOLETED:  Inactive notification that has been rendered obsolete by the mere passage of time since the first delivery attempt."
    
    @classmethod
    def getNonRetryableDeliveryStates(cls):
        return [cls.DELIVERY_IN_PROGRESS,
                cls.DELIVERY_CONFIRMED,
                cls.READ_CONFIRMED,
                cls.DELIVERY_FAILURE,
                cls.OBSOLETED,
                cls.EXPIRED]

class NotificationDeliveryConfirmationType(DeclEnum):
    """
    The type of event that caused the notification to be created
    """
    NONE = "NONE", "TNo confirmation was recieved"
    LINK_FOLLOWED = "LINK_FOLLOWED", "The user followed a link in the notification"
    NOTIFICATION_DISMISSED = "NOTIFICATION_DISMISSED", "The user dismissed the notification"

class NotificationClasses():

    ABSTRACT_NOTIFICATION = "ABSTRACT_NOTIFICATION"
    ABSTRACT_NOTIFICATION_ON_POST = "ABSTRACT_NOTIFICATION_ON_POST"
    NOTIFICATION_ON_POST_CREATED = "NOTIFICATION_ON_POST_CREATED"
    ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_POST = "ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_POST"
    ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_IDEA = "ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_IDEA"
    ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_EXTRACT = "ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_EXTRACT"
    ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_USERACCOUNT = "ABSTRACT_NOTIFICATION_SUBSCRIPTION_ON_USERACCOUNT"
class UnverifiedEmailException(Exception):
    pass
class Notification(Base):
    """
    A notification
    """
    __tablename__ = "notification"
    __mapper_args__ = {
        'polymorphic_identity': NotificationClasses.ABSTRACT_NOTIFICATION,
        'polymorphic_on': 'sqla_type',
        'with_polymorphic': '*'
    }
    id = Column(
        Integer,
    primary_key=True)

    sqla_type = Column(
        String,
        nullable=False,
        index=True)

    first_matching_subscription_id = Column(
        Integer,
        ForeignKey(
            'notification_subscription.id',
            ondelete = 'CASCADE', #Apparently, virtuoso doesn't suport ondelete RESTRICT
            onupdate = 'CASCADE'
            ),
        nullable=False, #Maybe should be true, not sure-benoitg
        doc="An annonymous pointer to the database object that originated the event")

    first_matching_subscription = relationship(
        NotificationSubscription,
        backref=backref('notifications', cascade="all, delete-orphan")
    )
    creation_date = Column(
        DateTime,
        nullable = False,
        default = datetime.utcnow)
    #user_id we can get it from the notification for "free"
    #Note:  The may be more than one interface to view notification, but we assume there is only one push method àt a time
    push_method =  Column(
        NotificationPushMethodType.db_type(),
        nullable = False,
        default = NotificationPushMethodType.EMAIL)
    push_address = Column(
        UnicodeText,
        nullable = True)
    push_date = Column(
        DateTime,
        nullable = True,
        default = None)
    delivery_state = Column(
        NotificationDeliveryStateType.db_type(),
        nullable = False,
        default = NotificationDeliveryStateType.QUEUED)
    delivery_confirmation = Column(
        NotificationDeliveryConfirmationType.db_type(),
        nullable = False,
        default = NotificationDeliveryConfirmationType.NONE)
    delivery_confirmation_date = Column(
        DateTime,
        nullable = True,
        default = datetime.utcnow)
    
    @abstractmethod
    def event_source_object(self):
        pass
    
    def event_source_type(self):
        return self.event_source_object().external_typename()
    
    def get_applicable_subscriptions(self):
        """ Fist matching_subscription is guaranteed to always be on top """
        #TODO: Store CRUDVERB
        applicableInstances = NotificationSubscription.findApplicableInstances(
            self.event_source_object().get_discussion_id(),
            CrudVerbs.CREATE, self.event_source_object(),
            self.first_matching_subscription.user)
        def sortSubscriptions(x,y):
            if x.id == self.first_matching_subscription_id:
                return -1
            elif y.id == self.first_matching_subscription_id:
                return 1
            else:
                return cmp(x.priority, y.priority)
        applicableInstances.sort(cmp=sortSubscriptions)
        return applicableInstances
    
    def render_to_email_html_part(self):
        """Override in child classes if your notification can be represented as
         email HTML part.  Otherwise return a falsy string (len must be defined)"""
        return False

    def render_to_email_text_part(self):
        """Override in child classes if your notification can be represented as
         email HTML part.  Otherwise return a falsy string (len must be defined)"""
        return ''
    
    @abstractmethod
    def get_notification_subject(self):
        """Typically for email"""

    def get_from_email_address(self):
        from_email = self.first_matching_subscription.discussion.admin_source.admin_sender
        assert from_email
        return from_email

    def get_to_email_address(self):
        """
        :raises: UnverifiedEmailException: If the prefered email isn't validated
        """
        prefered_email_account = self.first_matching_subscription.user.get_preferred_email_account()
        if not prefered_email_account.verified:
            raise UnverifiedEmailException("Email account for email "+ prefered_email_account.email +"is not verified")
        to_email = prefered_email_account.email
        assert to_email
        return to_email
    
    def render_to_email(self):

        email_text_part = self.render_to_email_text_part()
        email_html_part = self.render_to_email_html_part()
        if not email_text_part and not email_html_part:
            return ''
        frontendUrls = FrontendUrls(self.first_matching_subscription.discussion)
        msg = email.mime.Multipart.MIMEMultipart('alternative')
        from email.header import Header
        msg['Precedence'] = 'list'
        
        msg['List-ID'] = self.first_matching_subscription.discussion.uri()
        msg['Date'] = email.Utils.formatdate()

        if isinstance(self.event_source_object(), Post):
            msg['Message-ID'] = "<"+self.event_source_object().message_id+">"
            if self.event_source_object().parent:
                msg['In-Reply-To'] = self.event_source_object().parent.message_id
        else:
            raise NotImplementedError("TODO:  Implement message id's for non-Post event_source")
        
        #Archived-At: A direct link to the archived form of an individual email message.
        msg['List-Subscribe'] = frontendUrls.getUserNotificationSubscriptionsConfigurationUrl()
        msg['List-Unsubscribe'] = frontendUrls.getUserNotificationSubscriptionsConfigurationUrl()
        msg['Subject'] = Header(self.get_notification_subject(), 'utf-8')


        msg['From'] = Header(self.event_source_object().creator.name + " <" + self.get_from_email_address() + ">", 'utf-8')
        msg['To'] = self.get_to_email_address()
        if email_text_part:
            msg.attach(SafeMIMEText(email_text_part.encode('utf-8'), 'plain', 'utf-8'))
        if email_html_part:
            msg.attach(SafeMIMEText(email_html_part.encode('utf-8'), 'html', 'utf-8'))
        
        return msg.as_string()

User.notifications = relationship(
    Notification, viewonly=True,
    secondary=NotificationSubscription.__mapper__.mapped_table)

class NotificationOnPost(Notification):

    __tablename__ = "notification_on_post"
    __mapper_args__ = {
        'polymorphic_identity': NotificationClasses.ABSTRACT_NOTIFICATION_ON_POST,
        'polymorphic_on': 'sqla_type',
        'with_polymorphic': '*'
    }

    id = Column(Integer, ForeignKey(
        Notification.id,
        ondelete='CASCADE',
        onupdate='CASCADE'
    ), primary_key=True)

    post_id = Column(
        Integer,
        ForeignKey(
            Post.id,
            ondelete='CASCADE',
             onupdate='CASCADE'),
        nullable = False)

    post = relationship(Post, backref=backref(
        "notifications_on_post", cascade="all, delete-orphan"))

    @abstractmethod
    def event_source_object(self):
        return self.post
    
    def get_notification_subject(self):
        return self.post.subject

class NotificationOnPostCreated(NotificationOnPost):
    __mapper_args__ = {
        'polymorphic_identity': NotificationClasses.NOTIFICATION_ON_POST_CREATED,
        'with_polymorphic': '*'
    }
    
    def event_source_object(self):
        return NotificationOnPost.event_source_object(self)
    
    def render_to_email_html_part(self):
        from premailer import Premailer
        ink_css_path = os.path.normpath(os.path.join(os.path.abspath(__file__), '..' , '..', 'static', 'js', 'bower', 'ink', 'css', 'ink.css'))
        ink_css = open(ink_css_path)
        assert ink_css
        template = jinja_env.get_template('notifications/post.jinja2')
        html = template.render(subscription=self.first_matching_subscription,
                    notification=self,
                    frontendUrls = FrontendUrls(self.first_matching_subscription.discussion),
                    ink_css=ink_css.read(),
                    )
        return Premailer(html).transform()
