# Copyright 2014 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Controllers for the Oppia exploration learner view."""

import json
import logging
import random

import jinja2

from constants import constants
from core.controllers import base
from core.domain import acl_decorators
from core.domain import classifier_services
from core.domain import collection_services
from core.domain import config_domain
from core.domain import dependency_registry
from core.domain import event_services
from core.domain import exp_domain
from core.domain import exp_services
from core.domain import feedback_services
from core.domain import interaction_registry
from core.domain import learner_progress_services
from core.domain import moderator_services
from core.domain import rating_services
from core.domain import recommendations_services
from core.domain import rights_manager
from core.domain import summary_services
from core.domain import user_services
import feconf
import utils

MAX_SYSTEM_RECOMMENDATIONS = 4

DEFAULT_TWITTER_SHARE_MESSAGE_PLAYER = config_domain.ConfigProperty(
    'default_twitter_share_message_player', {
        'type': 'unicode',
    },
    'Default text for the Twitter share message for the learner view',
    default_value=(
        'Check out this interactive lesson from Oppia - a free, open-source '
        'learning platform!'))


def _get_exploration_player_data(
        exploration_id, version, collection_id, can_edit):
    try:
        exploration = exp_services.get_exploration_by_id(
            exploration_id, version=version)
    except Exception:
        raise Exception

    collection_title = None
    if collection_id:
        try:
            collection = collection_services.get_collection_by_id(
                collection_id)
            collection_title = collection.title
        except Exception:
            raise Exception

    version = exploration.version

    # TODO(sll): Cache these computations.
    interaction_ids = exploration.get_interaction_ids()
    dependency_ids = (
        interaction_registry.Registry.get_deduplicated_dependency_ids(
            interaction_ids))
    dependencies_html, additional_angular_modules = (
        dependency_registry.Registry.get_deps_html_and_angular_modules(
            dependency_ids))

    interaction_templates = (
        interaction_registry.Registry.get_interaction_html(
            interaction_ids))

    return {
        'INTERACTION_SPECS': interaction_registry.Registry.get_all_specs(),
        'DEFAULT_TWITTER_SHARE_MESSAGE_PLAYER': (
            DEFAULT_TWITTER_SHARE_MESSAGE_PLAYER.value),
        'additional_angular_modules': additional_angular_modules,
        'can_edit': can_edit,
        'dependencies_html': jinja2.utils.Markup(
            dependencies_html),
        'exploration_title': exploration.title,
        'exploration_version': version,
        'collection_id': collection_id,
        'collection_title': collection_title,
        'interaction_templates': jinja2.utils.Markup(
            interaction_templates),
        'is_private': rights_manager.is_exploration_private(
            exploration_id),
        # Note that this overwrites the value in base.py.
        'meta_name': exploration.title,
        # Note that this overwrites the value in base.py.
        'meta_description': utils.capitalize_string(exploration.objective),
        'nav_mode': feconf.NAV_MODE_EXPLORE,
    }


class ExplorationPageEmbed(base.BaseHandler):
    """Page describing a single embedded exploration."""

    @acl_decorators.can_play_exploration
    def get(self, exploration_id):
        """Handles GET requests."""
        version_str = self.request.get('v')
        version = int(version_str) if version_str else None
        exploration_rights = rights_manager.get_exploration_rights(
            exploration_id, strict=False)

        # Note: this is an optional argument and will be None when the
        # exploration is being played outside the context of a collection.
        collection_id = self.request.get('collection_id')
        can_edit = rights_manager.check_can_edit_activity(
            self.user, exploration_rights)

        # This check is needed in order to show the correct page when a 404
        # error is raised. The self.request.get('iframed') part of the check is
        # needed for backwards compatibility with older versions of the
        # embedding script.
        if (feconf.EXPLORATION_URL_EMBED_PREFIX in self.request.uri or
                self.request.get('iframed')):
            self.values['iframed'] = True
        try:
            # If the exploration does not exist, a 404 error is raised.
            exploration_data_values = _get_exploration_player_data(
                exploration_id, version, collection_id, can_edit)
        except Exception:
            raise self.PageNotFoundException

        self.values.update(exploration_data_values)
        self.values['iframed'] = True
        self.render_template(
            'pages/exploration_player/exploration_player.html',
            iframe_restriction=None)


class ExplorationPage(base.BaseHandler):
    """Page describing a single exploration."""

    @acl_decorators.can_play_exploration
    def get(self, exploration_id):
        """Handles GET requests."""
        version_str = self.request.get('v')
        version = int(version_str) if version_str else None
        exploration_rights = rights_manager.get_exploration_rights(
            exploration_id, strict=False)

        if self.request.get('iframed'):
            redirect_url = '/embed/exploration/%s' % exploration_id
            if version_str:
                redirect_url += '?v=%s' % version_str
            self.redirect(redirect_url)
            return

        # Note: this is an optional argument and will be None when the
        # exploration is being played outside the context of a collection.
        collection_id = self.request.get('collection_id')
        can_edit = rights_manager.check_can_edit_activity(
            self.user, exploration_rights)

        try:
            # If the exploration does not exist, a 404 error is raised.
            exploration_data_values = _get_exploration_player_data(
                exploration_id, version, collection_id, can_edit)
        except Exception:
            raise self.PageNotFoundException

        self.values.update(exploration_data_values)
        self.values['iframed'] = False
        self.render_template(
            'pages/exploration_player/exploration_player.html')


class ExplorationHandler(base.BaseHandler):
    """Provides the initial data for a single exploration."""

    GET_HANDLER_ERROR_RETURN_TYPE = feconf.HANDLER_TYPE_JSON

    @acl_decorators.can_play_exploration
    def get(self, exploration_id):
        """Populates the data on the individual exploration page."""
        version = self.request.get('v')
        version = int(version) if version else None

        try:
            exploration = exp_services.get_exploration_by_id(
                exploration_id, version=version)
        except Exception as e:
            raise self.PageNotFoundException(e)

        exploration_rights = rights_manager.get_exploration_rights(
            exploration_id, strict=False)
        user_settings = user_services.get_user_settings(self.user_id)

        preferred_audio_language_code = None
        if user_settings is not None:
            preferred_audio_language_code = (
                user_settings.preferred_audio_language_code)

        # Retrieve all classifiers for the exploration.
        state_classifier_mapping = {}
        classifier_training_jobs = (
            classifier_services.get_classifier_training_jobs(
                exploration_id, exploration.version, exploration.states))
        for index, state_name in enumerate(exploration.states):
            if classifier_training_jobs[index] is not None:
                classifier_data = classifier_training_jobs[
                    index].classifier_data
                algorithm_id = classifier_training_jobs[index].algorithm_id
                data_schema_version = (
                    classifier_training_jobs[index].data_schema_version)
                state_classifier_mapping[state_name] = {
                    'algorithm_id': algorithm_id,
                    'classifier_data': classifier_data,
                    'data_schema_version': data_schema_version
                }

        self.values.update({
            'can_edit': (
                rights_manager.check_can_edit_activity(
                    self.user, exploration_rights)),
            'exploration': exploration.to_player_dict(),
            'exploration_id': exploration_id,
            'is_logged_in': bool(self.user_id),
            'session_id': utils.generate_new_session_id(),
            'version': exploration.version,
            'preferred_audio_language_code': preferred_audio_language_code,
            'state_classifier_mapping': state_classifier_mapping
        })
        self.render_json(self.values)


class AnswerSubmittedEventHandler(base.BaseHandler):
    """Tracks a learner submitting an answer."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @acl_decorators.can_play_exploration
    def post(self, exploration_id):
        old_state_name = self.payload.get('old_state_name')
        # The reader's answer.
        answer = self.payload.get('answer')
        # Parameters associated with the learner.
        params = self.payload.get('params', {})
        # The version of the exploration.
        version = self.payload.get('version')
        session_id = self.payload.get('session_id')
        client_time_spent_in_secs = self.payload.get(
            'client_time_spent_in_secs')
        # The answer group and rule spec indexes, which will be used to get
        # the rule spec string.
        answer_group_index = self.payload.get('answer_group_index')
        rule_spec_index = self.payload.get('rule_spec_index')
        classification_categorization = self.payload.get(
            'classification_categorization')

        exploration = exp_services.get_exploration_by_id(
            exploration_id, version=version)

        old_interaction = exploration.states[old_state_name].interaction

        old_interaction_instance = (
            interaction_registry.Registry.get_interaction_by_id(
                old_interaction.id))

        normalized_answer = old_interaction_instance.normalize_answer(answer)

        event_services.AnswerSubmissionEventHandler.record(
            exploration_id, version, old_state_name,
            exploration.states[old_state_name].interaction.id,
            answer_group_index, rule_spec_index, classification_categorization,
            session_id, client_time_spent_in_secs, params, normalized_answer)
        self.render_json({})


class StateHitEventHandler(base.BaseHandler):
    """Tracks a learner hitting a new state."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @acl_decorators.can_play_exploration
    def post(self, exploration_id):
        """Handles POST requests."""
        new_state_name = self.payload.get('new_state_name')
        exploration_version = self.payload.get('exploration_version')
        session_id = self.payload.get('session_id')
        # TODO(sll): why do we not record the value of this anywhere?
        client_time_spent_in_secs = self.payload.get(  # pylint: disable=unused-variable
            'client_time_spent_in_secs')
        old_params = self.payload.get('old_params')
        is_first_hit = self.payload.get('is_first_hit')

        # Record the state hit, if it is not the END state.
        if new_state_name is not None:
            event_services.StateHitEventHandler.record(
                exploration_id, exploration_version, new_state_name,
                session_id, old_params, feconf.PLAY_TYPE_NORMAL, is_first_hit)
        else:
            logging.error('Unexpected StateHit event for the END state.')
        self.render_json({})


class StateCompleteEventHandler(base.BaseHandler):
    """Tracks a learner complete a state. Here, 'completing' means answering
    the state and progressing to a new state.
    """

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @acl_decorators.can_play_exploration
    def post(self, exploration_id):
        """Handles POST requests."""
        if feconf.ENABLE_NEW_STATS_FRAMEWORK:
            event_services.StateCompleteEventHandler.record(
                exploration_id, self.payload.get('exp_version'),
                self.payload.get('state_name'), self.payload.get('session_id'),
                self.payload.get('time_spent_in_state_secs'))
        self.render_json({})


class ClassifyHandler(base.BaseHandler):
    """Stateless handler that performs a classify() operation server-side and
    returns the corresponding classification result, which is a dict containing
    three keys:
        'outcome': A dict representing the outcome of the answer group matched.
        'answer_group_index': The index of the matched answer group.
        'rule_spec_index': The index of the matched rule spec in the matched
            answer group.
    """

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @acl_decorators.can_play_exploration
    def post(self, unused_exploration_id):
        """Handle POST requests.

        Note: unused_exploration_id is needed because
            @acl_decorators.can_play_exploration needs 2 arguments.
        """
        # A domain object representing the old state.
        old_state = exp_domain.State.from_dict(self.payload.get('old_state'))
        # The learner's raw answer.
        answer = self.payload.get('answer')
        # The learner's parameter values.
        params = self.payload.get('params')
        params['answer'] = answer
        result = classifier_services.classify(old_state, answer)
        self.render_json(result)


class ReaderFeedbackHandler(base.BaseHandler):
    """Submits feedback from the reader."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @acl_decorators.can_play_exploration
    def post(self, exploration_id):
        """Handles POST requests."""
        state_name = self.payload.get('state_name')
        subject = self.payload.get('subject', 'Feedback from a learner')
        feedback = self.payload.get('feedback')
        include_author = self.payload.get('include_author')

        feedback_services.create_thread(
            exploration_id,
            state_name,
            self.user_id if include_author else None,
            subject,
            feedback)
        self.render_json(self.values)


class ExplorationStartEventHandler(base.BaseHandler):
    """Tracks a learner starting an exploration."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @acl_decorators.can_play_exploration
    def post(self, exploration_id):
        """Handles POST requests."""
        event_services.StartExplorationEventHandler.record(
            exploration_id, self.payload.get('version'),
            self.payload.get('state_name'),
            self.payload.get('session_id'),
            self.payload.get('params'),
            feconf.PLAY_TYPE_NORMAL)
        self.render_json({})


class ExplorationActualStartEventHandler(base.BaseHandler):
    """Tracks a learner actually starting an exploration. These are the learners
    who traverse past the initial state.
    """

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @acl_decorators.can_play_exploration
    def post(self, exploration_id):
        """Handles POST requests."""
        if feconf.ENABLE_NEW_STATS_FRAMEWORK:
            event_services.ExplorationActualStartEventHandler.record(
                exploration_id, self.payload.get('exploration_version'),
                self.payload.get('state_name'), self.payload.get('session_id'))
        self.render_json({})


class SolutionHitEventHandler(base.BaseHandler):
    """Tracks a learner clicking on the 'View Solution' button."""

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @acl_decorators.can_play_exploration
    def post(self, exploration_id):
        """Handles POST requests."""
        if feconf.ENABLE_NEW_STATS_FRAMEWORK:
            event_services.SolutionHitEventHandler.record(
                exploration_id, self.payload.get('exploration_version'),
                self.payload.get('state_name'), self.payload.get('session_id'),
                self.payload.get('time_spent_in_state_secs'))
        self.render_json({})


class ExplorationCompleteEventHandler(base.BaseHandler):
    """Tracks a learner completing an exploration.

    The state name recorded should be a state with a terminal interaction.
    """

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @acl_decorators.can_play_exploration
    def post(self, exploration_id):
        """Handles POST requests."""

        # This will be None if the exploration is not being played within the
        # context of a collection.
        collection_id = self.payload.get('collection_id')
        user_id = self.user_id

        event_services.CompleteExplorationEventHandler.record(
            exploration_id,
            self.payload.get('version'),
            self.payload.get('state_name'),
            self.payload.get('session_id'),
            self.payload.get('client_time_spent_in_secs'),
            self.payload.get('params'),
            feconf.PLAY_TYPE_NORMAL)

        if user_id:
            learner_progress_services.mark_exploration_as_completed(
                user_id, exploration_id)

        if user_id and collection_id:
            collection_services.record_played_exploration_in_collection_context(
                user_id, collection_id, exploration_id)
            collections_left_to_complete = (
                collection_services.get_next_exploration_ids_to_complete_by_user( # pylint: disable=line-too-long
                    user_id, collection_id))

            if not collections_left_to_complete:
                learner_progress_services.mark_collection_as_completed(
                    user_id, collection_id)
            else:
                learner_progress_services.mark_collection_as_incomplete(
                    user_id, collection_id)

        self.render_json(self.values)


class ExplorationMaybeLeaveHandler(base.BaseHandler):
    """Tracks a learner leaving an exploration without completing it.

    The state name recorded should be a state with a non-terminal interaction.
    """

    REQUIRE_PAYLOAD_CSRF_CHECK = False

    @acl_decorators.can_play_exploration
    def post(self, exploration_id):
        """Handles POST requests."""
        version = self.payload.get('version')
        state_name = self.payload.get('state_name')
        user_id = self.user_id
        collection_id = self.payload.get('collection_id')

        if user_id:
            learner_progress_services.mark_exploration_as_incomplete(
                user_id, exploration_id, state_name, version)

        if user_id and collection_id:
            learner_progress_services.mark_collection_as_incomplete(
                user_id, collection_id)

        event_services.MaybeLeaveExplorationEventHandler.record(
            exploration_id,
            version,
            state_name,
            self.payload.get('session_id'),
            self.payload.get('client_time_spent_in_secs'),
            self.payload.get('params'),
            feconf.PLAY_TYPE_NORMAL)
        self.render_json(self.values)


class LearnerIncompleteActivityHandler(base.BaseHandler):
    """Handles operations related to the activities in the incomplete list of
    the user.
    """
    @acl_decorators.can_access_learner_dashboard
    def delete(self, activity_type, activity_id):
        if activity_type == constants.ACTIVITY_TYPE_EXPLORATION:
            learner_progress_services.remove_exp_from_incomplete_list(
                self.user_id, activity_id)
        elif activity_type == constants.ACTIVITY_TYPE_COLLECTION:
            learner_progress_services.remove_collection_from_incomplete_list(
                self.user_id, activity_id)

        self.render_json(self.values)


class RatingHandler(base.BaseHandler):
    """Records the rating of an exploration submitted by a user.

    Note that this represents ratings submitted on completion of the
    exploration.
    """

    GET_HANDLER_ERROR_RETURN_TYPE = feconf.HANDLER_TYPE_JSON

    @acl_decorators.can_play_exploration
    def get(self, exploration_id):
        """Handles GET requests."""
        self.values.update({
            'overall_ratings':
                rating_services.get_overall_ratings_for_exploration(
                    exploration_id),
            'user_rating': (
                rating_services.get_user_specific_rating_for_exploration(
                    self.user_id, exploration_id) if self.user_id else None)
        })
        self.render_json(self.values)

    @acl_decorators.can_rate_exploration
    def put(self, exploration_id):
        """Handles PUT requests for submitting ratings at the end of an
        exploration.
        """
        user_rating = self.payload.get('user_rating')
        rating_services.assign_rating_to_exploration(
            self.user_id, exploration_id, user_rating)
        self.render_json({})


class RecommendationsHandler(base.BaseHandler):
    """Provides recommendations to be displayed at the end of explorations.
    Which explorations are provided depends on whether the exploration was
    played within the context of a collection and whether the user is logged in.
    If both are true, then the explorations are suggested from the collection,
    if there are upcoming explorations for the learner to complete.
    """

    GET_HANDLER_ERROR_RETURN_TYPE = feconf.HANDLER_TYPE_JSON

    @acl_decorators.can_play_exploration
    def get(self, exploration_id):
        """Handles GET requests."""
        collection_id = self.request.get('collection_id')
        include_system_recommendations = self.request.get(
            'include_system_recommendations')
        try:
            author_recommended_exp_ids = json.loads(self.request.get(
                'stringified_author_recommended_ids'))
        except Exception:
            raise self.PageNotFoundException

        auto_recommended_exp_ids = []
        if self.user_id and collection_id:
            next_exp_ids_in_collection = (
                collection_services.get_next_exploration_ids_to_complete_by_user(  # pylint: disable=line-too-long
                    self.user_id, collection_id))
            auto_recommended_exp_ids = list(
                set(next_exp_ids_in_collection) -
                set(author_recommended_exp_ids))
        else:
            next_exp_ids_in_collection = []
            if collection_id:
                collection = collection_services.get_collection_by_id(
                    collection_id)
                next_exp_ids_in_collection = (
                    collection.get_next_exploration_ids_in_sequence(
                        exploration_id))
            if next_exp_ids_in_collection:
                auto_recommended_exp_ids = list(
                    set(next_exp_ids_in_collection) -
                    set(author_recommended_exp_ids))
            elif include_system_recommendations:
                system_chosen_exp_ids = (
                    recommendations_services.get_exploration_recommendations(
                        exploration_id))
                filtered_exp_ids = list(
                    set(system_chosen_exp_ids) -
                    set(author_recommended_exp_ids))
                auto_recommended_exp_ids = random.sample(
                    filtered_exp_ids,
                    min(MAX_SYSTEM_RECOMMENDATIONS, len(filtered_exp_ids)))

        self.values.update({
            'summaries': (
                summary_services.get_displayable_exp_summary_dicts_matching_ids(
                    author_recommended_exp_ids + auto_recommended_exp_ids)),
        })
        self.render_json(self.values)


class FlagExplorationHandler(base.BaseHandler):
    """Handles operations relating to learner flagging of explorations."""

    @acl_decorators.can_flag_exploration
    def post(self, exploration_id):
        moderator_services.enqueue_flag_exploration_email_task(
            exploration_id,
            self.payload.get('report_text'),
            self.user_id)
        self.render_json(self.values)
