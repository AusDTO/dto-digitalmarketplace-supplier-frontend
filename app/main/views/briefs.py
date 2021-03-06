# coding: utf-8
from __future__ import unicode_literals

import re
import json

from flask import abort, flash, redirect, render_template, request, url_for, \
    current_app
from flask_login import current_user
import flask_featureflags as feature

from dmapiclient import HTTPError
from dmutils.forms import render_template_with_csrf
from dmutils.email import send_email, EmailError
from dmutils.documents import upload_service_documents
from dmutils.s3 import S3
from react.render import render_component

import six
import rollbar

from ..helpers import login_required
from ..helpers.briefs import (
    get_brief,
    is_supplier_selected_for_brief,
    is_supplier_not_eligible_for_brief,
    send_brief_clarification_question,
    supplier_has_a_brief_response,
    supplier_is_assessed,
    supplier_is_unassessed
)
from ..helpers.frameworks import get_framework_and_lot
from ...main import main, content_loader
from ... import data_api_client


@main.route('/opportunities/<int:brief_id>/question-and-answer-session', methods=['GET'])
@login_required
def question_and_answer_session(brief_id):

    brief = get_brief(data_api_client, brief_id, allowed_statuses=['live'])
    if brief['clarificationQuestionsAreClosed']:
        return render_template(
            "briefs/brief_closed_error.html"
        ), 400

    if not is_supplier_selected_for_brief(data_api_client, current_user, brief):
        return _render_not_selected_for_brief_error_page(clarification_question=True)

    return render_template(
        "briefs/question_and_answer_session.html",
        brief=brief,
    ), 200


@main.route('/opportunities/<int:brief_id>/ask-a-question', methods=['GET'])
@login_required
def ask_brief_clarification_question(brief_id):
    return redirect('/2/brief/{}/ask-a-question'.format(brief_id))


@main.route('/opportunities/<int:brief_id>/responses/create', methods=['GET'])
@login_required
def brief_response(brief_id):
    brief = get_brief(data_api_client, brief_id)
    if brief['status'] != 'live':
        return render_template(
            "briefs/brief_closed_error.html"
        ), 400

    if not is_supplier_selected_for_brief(data_api_client, current_user, brief):
        return _render_not_selected_for_brief_error_page()

    ineligible = is_supplier_not_eligible_for_brief(data_api_client, current_user.supplier_code, brief)
    if ineligible:
        return _render_error_page(ineligible, brief)

    if supplier_has_a_brief_response(data_api_client, current_user.supplier_code, brief_id):
        flash('already_applied', 'error')
        return redirect(url_for(".view_response_result", brief_id=brief_id))

    if brief['frameworkSlug'] == 'digital-marketplace':
        current_supplier = data_api_client.req.suppliers(current_user.supplier_code).get()
        if brief['lotSlug'] == 'digital-outcome':
            if len(current_supplier['supplier']['domains'].get('assessed', [])) == 0:
                return redirect(url_for(".choose_assessment", brief_id=brief_id))
        else:
            if not supplier_is_assessed(current_supplier, brief['areaOfExpertise']):
                current_domain = data_api_client.req.domain(brief['areaOfExpertise']).get()
                if supplier_is_unassessed(current_supplier, brief['areaOfExpertise']):
                    return redirect(url_for(".create_assessment", brief_id=brief_id,
                                            domain_id=current_domain['domain']['id']))
                else:
                    return _render_error_page('briefs/missing_domain.html', brief)

    framework, lot = get_framework_and_lot(
        data_api_client, brief['frameworkSlug'], brief['lotSlug'], allowed_statuses=['live'])

    content = content_loader.get_manifest(framework['slug'], 'edit_brief_response').filter({'lot': lot['slug']})
    section = content.get_section(content.get_next_editable_section_id())

    # replace generic 'Apply for opportunity' title with title including the name of the brief
    section.name = "Apply for ‘{}’".format(brief['title'])
    section.inject_brief_questions_into_boolean_list_question(brief)

    return render_template_with_csrf(
        "briefs/brief_response.html",
        brief=brief,
        lot_slug=brief['lotSlug'],
        service_data={},
        section=section,
    )


def send_thank_you_email_to_responders(brief, brief_response, brief_response_url):
    ess = brief.get('essentialRequirements', [])
    nth = brief.get('niceToHaveRequirements', [])

    ess = zip(ess, brief_response.get('essentialRequirements', []))
    nth = zip(nth, brief_response.get('niceToHaveRequirements', []))

    to_email_address = brief_response['respondToEmailAddress']

    email_body = render_template(
        'emails/brief_response_submitted.html',
        brief=brief,
        essential_requirements=ess,
        nice_to_have_requirements=nth,
        brief_response=brief_response,
        brief_response_url=brief_response_url,
        frontend_address=current_app.config['FRONTEND_ADDRESS']
    )

    try:
        send_email(
            to_email_address,
            email_body,
            'We\'ve received your application',
            current_app.config['DM_GENERIC_NOREPLY_EMAIL'],
            current_app.config['DM_GENERIC_SUPPORT_NAME'],
        )
    except EmailError as e:
        rollbar.report_exc_info()
        current_app.logger.error(
            'seller response received email failed to send. '
            'error {error}',
            extra={
                'error': six.text_type(e), })
        abort(503, response='Failed to send seller response received email.')


# Add a create route
@main.route('/opportunities/<int:brief_id>/responses/create', methods=['POST'])
@login_required
def submit_brief_response(brief_id):
    """Hits up the data API to create a new brief response."""

    brief = get_brief(data_api_client, brief_id)

    if brief['status'] != 'live':
        return render_template(
            "briefs/brief_closed_error.html"
        ), 400

    if not is_supplier_selected_for_brief(data_api_client, current_user, brief):
        return _render_not_selected_for_brief_error_page()

    ineligible = is_supplier_not_eligible_for_brief(data_api_client, current_user.supplier_code, brief)
    if ineligible:
        return _render_error_page(ineligible, brief, clarification_question=True)

    if supplier_has_a_brief_response(data_api_client, current_user.supplier_code, brief_id):
        flash('already_applied', 'error')
        return redirect(url_for(".view_response_result", brief_id=brief_id))

    framework, lot = get_framework_and_lot(
        data_api_client, brief['frameworkSlug'], brief['lotSlug'], allowed_statuses=['live'])

    content = content_loader.get_manifest(framework['slug'], 'edit_brief_response').filter({'lot': lot['slug']})
    section = content.get_section(content.get_next_editable_section_id())
    response_data = section.get_data(request.form)

    service = {'frameworkSlug': brief['frameworkSlug'], 'supplierCode': current_user.supplier_code, 'id': brief_id}
    uploaded_documents, document_errors = upload_service_documents(current_app.config.get('S3_BUCKET_NAME'),
                                                                   "", service, request.files, section)

    if document_errors:
        # replace generic 'Apply for opportunity' title with title including the name of the brief
        section.name = "Apply for ‘{}’".format(brief['title'])
        section.inject_brief_questions_into_boolean_list_question(brief)
        section_summary = section.summary(response_data)
        errors = section_summary.get_error_messages(document_errors)
        if errors.get('attachedDocumentURL', {}).get('question'):
            errors['attachedDocumentURL']['question'] = "Attached document"
        return render_template_with_csrf(
            "briefs/brief_response.html",
            status_code=400,
            brief=brief,
            service_data=response_data,
            section=section,
            errors=errors,
        )
    else:
        response_data.update(uploaded_documents)

    try:
        brief_response = data_api_client.create_brief_response(
            brief_id, current_user.supplier_code, response_data, current_user.email_address
        )['briefResponses']
    except HTTPError as e:
        # replace generic 'Apply for opportunity' title with title including the name of the brief
        section.name = "Apply for ‘{}’".format(brief['title'])
        section.inject_brief_questions_into_boolean_list_question(brief)
        section_summary = section.summary(response_data)

        errors = {}
        if isinstance(e.message, dict):
            errors = section_summary.get_error_messages(e.message)
            rollbar.report_message(json.dumps(errors), 'error', request)
        else:
            flash('already_applied', 'error')

        return render_template_with_csrf(
            "briefs/brief_response.html",
            status_code=400,
            brief=brief,
            service_data=response_data,
            section=section,
            errors=errors,
        )

    response_url = url_for(".view_response_result", brief_id=brief_id, result='success')

    send_thank_you_email_to_responders(brief, brief_response, response_url)

    return redirect(response_url)


@main.route('/opportunities/<int:brief_id>/responses/result')
@login_required
def view_response_result(brief_id):
    brief = get_brief(data_api_client, brief_id, allowed_statuses=['live'])

    if not is_supplier_selected_for_brief(data_api_client, current_user, brief):
        return _render_not_selected_for_brief_error_page()

    ineligible = is_supplier_not_eligible_for_brief(data_api_client, current_user.supplier_code, brief)
    if ineligible:
        return _render_error_page(ineligible, brief, clarification_question=True)

    brief_response = data_api_client.find_brief_responses(
        brief_id=brief_id,
        supplier_code=current_user.supplier_code
    )['briefResponses']

    # A check for training lot is required because essentialRequirements is not
    # included in training responses
    if len(brief_response) == 0:
        return redirect(url_for(".brief_response", brief_id=brief_id))
    elif brief['lotSlug'] == 'training' or all(brief_response[0]['essentialRequirements']):
        result_state = 'submitted_ok'
    else:
        result_state = 'submitted_unsuccessful'

    brief_response = brief_response[0]
    framework, lot = get_framework_and_lot(
        data_api_client, brief['frameworkSlug'], brief['lotSlug'], allowed_statuses=['live'])

    response_content = content_loader.get_manifest(
        framework['slug'], 'display_brief_response').filter({'lot': lot['slug']})
    for section in response_content:
        section.inject_brief_questions_into_boolean_list_question(brief)

    brief_content = content_loader.get_manifest(
        framework['slug'], 'edit_brief').filter({'lot': lot['slug']})
    brief_summary = brief_content.summary(brief)

    return render_template(
        'briefs/view_response_result.html',
        brief=brief,
        brief_summary=brief_summary,
        brief_response=brief_response,
        result_state=result_state,
        response_content=response_content
    )


def _render_not_selected_for_brief_error_page(clarification_question=False):
    return render_template(
        "briefs/not_is_supplier_selected_for_brief_error.html",
        clarification_question=clarification_question,
    ), 400


def _render_error_page(eligibleTemplate, brief, clarification_question=False,):
    return render_template(
        eligibleTemplate,
        clarification_question=clarification_question,
        framework_name=brief['frameworkSlug'],
        lot_name=brief['lotSlug'],
        domain=brief.get('areaOfExpertise', ''),
    ), 400


def _render_not_eligible_for_brief_error_page(brief, clarification_question=False):
    return render_template(
        "briefs/not_is_supplier_eligible_for_brief_error.html",
        clarification_question=clarification_question,
        framework_name=brief['frameworkSlug'],
        lot_name=brief['lotSlug'],
        domain=brief.get('areaOfExpertise', '')
    ), 400


@main.route('/opportunities/<int:brief_id>/assessment/choose', methods=['GET'])
@login_required
def choose_assessment(brief_id):
    return redirect('/2/seller-dashboard')


@main.route('/opportunities/<int:brief_id>/assessment', methods=['GET'])
@main.route('/opportunities/<int:brief_id>/assessment/<int:domain_id>', methods=['GET'])
@login_required
def create_assessment(brief_id, domain_id=None):
    if request.args.get('local.domain'):
        domain_id = int(request.args.get('local.domain'))
    if request.args.get('domain'):
        domain_id = int(request.args.get('domain'))
    if domain_id is None:
        return abort(400)
    return redirect('/2/seller-assessment/create/{}'.format(domain_id))


@main.route('/opportunities/<int:brief_id>/assessment/status', methods=['GET'])
@login_required
def assessment_status(brief_id):
    return redirect('/2/seller-dashboard')


@main.route('/opportunities/<int:brief_id>/assessment/initial', methods=['GET'])
@login_required
def assessment_initial(brief_id):
    return redirect('/2/seller-dashboard')
