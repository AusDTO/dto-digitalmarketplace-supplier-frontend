from flask_login import current_user
from flask import render_template, request, redirect, url_for, abort, flash, current_app
import flask_featureflags

from ... import data_api_client
from ...main import main, content_loader
from ..helpers import login_required
from ..helpers.services import is_service_associated_with_supplier, \
    get_signed_document_url, count_unanswered_questions, get_next_section_name
from ..helpers.frameworks import get_framework_and_lot, get_declaration_status

from dmapiclient import HTTPError
from dmutils import s3
from dmutils.documents import upload_service_documents
from dmutils.forms import render_template_with_csrf


@main.route('/services')
@login_required
def list_services():
    suppliers_services = data_api_client.find_services(supplier_code=current_user.supplier_code)["services"]
    sorted_services = sorted(
        suppliers_services,
        key=lambda service: service['frameworkSlug'],
        reverse=True
    )

    return render_template(
        "services/list_services.html",
        services=sorted_services), 200


#  #######################  EDITING LIVE SERVICES #############################


@main.route('/services/<string:service_id>', methods=['GET'])
@login_required
def edit_service(service_id):
    service = data_api_client.get_service(service_id)
    service_unavailability_information = service.get('serviceMadeUnavailableAuditEvent')
    service = service.get('services')

    if not is_service_associated_with_supplier(service):
        abort(404)

    framework = data_api_client.get_framework(service['frameworkSlug'])['frameworks']

    content = content_loader.get_manifest(framework['slug'], 'edit_service').filter(service)
    remove_requested = True if request.args.get('remove_requested') else False

    return render_template_with_csrf(
        "services/service.html",
        service_id=service.get('id'),
        service_data=service,
        service_unavailability_information=service_unavailability_information,
        framework=framework,
        sections=content.summary(service),
        remove_requested=remove_requested
    )


@main.route('/services/<string:service_id>/remove', methods=['POST'])
@login_required
def remove_service(service_id):
    service = data_api_client.get_service(service_id).get('services')

    if not is_service_associated_with_supplier(service):
        abort(404)

    # suppliers can't un-remove a service
    if service.get('status') != 'published':
        abort(400)

    if request.form.get('remove_confirmed'):

        updated_service = data_api_client.update_service_status(
            service.get('id'),
            'enabled',
            current_user.email_address)

        updated_service = updated_service.get('services')

        flash({
            'updated_service_name': updated_service.get('serviceName')
        }, 'remove_service')

        return redirect(url_for(".list_services"))

    return redirect(url_for(
        ".edit_service",
        service_id=service_id,
        remove_requested=True))


@main.route('/services/<string:service_id>/edit/<string:section_id>', methods=['GET'])
@login_required
@flask_featureflags.is_active_feature('EDIT_SECTIONS')
def edit_section(service_id, section_id):
    service = data_api_client.get_service(service_id)
    if service is None:
        abort(404)
    service = service['services']

    if not is_service_associated_with_supplier(service):
        abort(404)

    content = content_loader.get_manifest('g-cloud-6', 'edit_service').filter(service)
    section = content.get_section(section_id)
    if section is None or not section.editable:
        abort(404)

    return render_template_with_csrf(
        "services/edit_section.html",
        section=section,
        service_data=service,
        service_id=service_id
    )


@main.route('/services/<string:service_id>/edit/<string:section_id>', methods=['POST'])
@login_required
@flask_featureflags.is_active_feature('EDIT_SECTIONS')
def update_section(service_id, section_id):
    service = data_api_client.get_service(service_id)
    if service is None:
        abort(404)
    service = service['services']

    if not is_service_associated_with_supplier(service):
        abort(404)

    content = content_loader.get_manifest('g-cloud-6', 'edit_service').filter(service)
    section = content.get_section(section_id)
    if section is None or not section.editable:
        abort(404)

    posted_data = section.get_data(request.form)

    try:
        data_api_client.update_service(
            service_id,
            posted_data,
            current_user.email_address)
    except HTTPError as e:
        errors = section.get_error_messages(e.message)
        if not posted_data.get('serviceName', None):
            posted_data['serviceName'] = service.get('serviceName', '')
        return render_template_with_csrf(
            "services/edit_section.html",
            section=section,
            service_data=posted_data,
            service_id=service_id,
            errors=errors
        )

    return redirect(url_for(".edit_service", service_id=service_id))


#  ####################  CREATING NEW DRAFT SERVICES ##########################


@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/create', methods=['GET'])
@login_required
def start_new_draft_service(framework_slug, lot_slug):
    """Page to kick off creation of a new service."""

    framework, lot = get_framework_and_lot(data_api_client, framework_slug, lot_slug, allowed_statuses=['open'])

    content = content_loader.get_manifest(framework_slug, 'edit_submission').filter(
        {'lot': lot['slug']}
    )

    section = content.get_section(content.get_next_editable_section_id())

    return render_template_with_csrf(
        "services/edit_submission_section.html",
        framework=framework,
        service_data={},
        section=section
    )


@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/create', methods=['POST'])
@login_required
def create_new_draft_service(framework_slug, lot_slug):
    """Hits up the data API to create a new draft service."""

    framework, lot = get_framework_and_lot(data_api_client, framework_slug, lot_slug, allowed_statuses=['open'])

    content = content_loader.get_manifest(framework_slug, 'edit_submission').filter(
        {'lot': lot['slug']}
    )

    section = content.get_section(content.get_next_editable_section_id())

    update_data = section.get_data(request.form)

    try:
        draft_service = data_api_client.create_new_draft_service(
            framework_slug, lot['slug'], current_user.supplier_code, update_data,
            current_user.email_address, page_questions=section.get_field_names()
        )['services']
    except HTTPError as e:
        update_data = section.unformat_data(update_data)
        errors = section.get_error_messages(e.message)

        return render_template_with_csrf(
            "services/edit_submission_section.html",
            status_code=400,
            framework=framework,
            section=section,
            service_data=update_data,
            errors=errors
        )

    return redirect(
        url_for(
            ".view_service_submission",
            framework_slug=framework['slug'],
            lot_slug=draft_service['lotSlug'],
            service_id=draft_service['id'],
        )
    )


@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/<service_id>/copy', methods=['POST'])
@login_required
def copy_draft_service(framework_slug, lot_slug, service_id):
    framework, lot = get_framework_and_lot(data_api_client, framework_slug, lot_slug, allowed_statuses=['open'])
    draft = data_api_client.get_draft_service(service_id).get('services')

    if draft['lotSlug'] != lot_slug or draft['frameworkSlug'] != framework_slug:
        abort(404)

    if not is_service_associated_with_supplier(draft):
        abort(404)

    content = content_loader.get_manifest(framework_slug, 'edit_submission').filter(
        {'lot': lot['slug']}
    )

    draft_copy = data_api_client.copy_draft_service(
        service_id,
        current_user.email_address
    )['services']

    return redirect(url_for(".edit_service_submission",
                            framework_slug=framework['slug'],
                            lot_slug=draft['lotSlug'],
                            service_id=draft_copy['id'],
                            section_id=content.get_next_editable_section_id(),
                            return_to_summary=1
                            ))


@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/<service_id>/complete', methods=['POST'])
@login_required
def complete_draft_service(framework_slug, lot_slug, service_id):
    framework, lot = get_framework_and_lot(data_api_client, framework_slug, lot_slug, allowed_statuses=['open'])
    draft = data_api_client.get_draft_service(service_id).get('services')

    if draft['lotSlug'] != lot_slug or draft['frameworkSlug'] != framework_slug:
        abort(404)

    if not is_service_associated_with_supplier(draft):
        abort(404)

    data_api_client.complete_draft_service(
        service_id,
        current_user.email_address
    )

    flash({
        'service_name': draft.get('serviceName') or draft.get('lotName'),
        'virtual_pageview_url': "{}/{}/{}".format(
            url_for(".framework_submission_lots", framework_slug=framework['slug']),
            lot_slug,
            "complete"
        )
    }, 'service_completed')

    if lot['oneServiceLimit']:
        return redirect(url_for(".framework_submission_lots", framework_slug=framework['slug']))
    else:
        return redirect(url_for(".framework_submission_services",
                                framework_slug=framework['slug'],
                                lot_slug=lot_slug,
                                lot=lot_slug))


@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/<service_id>/delete', methods=['POST'])
@login_required
def delete_draft_service(framework_slug, lot_slug, service_id):
    framework, lot = get_framework_and_lot(data_api_client, framework_slug, lot_slug, allowed_statuses=['open'])
    draft = data_api_client.get_draft_service(service_id).get('services')

    if draft['lotSlug'] != lot_slug or draft['frameworkSlug'] != framework_slug:
        abort(404)

    if not is_service_associated_with_supplier(draft):
        abort(404)

    if request.form.get('delete_confirmed') == 'true':
        data_api_client.delete_draft_service(
            service_id,
            current_user.email_address
        )

        flash({'service_name': draft.get('serviceName', draft['lotName'])}, 'service_deleted')
        if lot['oneServiceLimit']:
            return redirect(url_for(".framework_submission_lots", framework_slug=framework['slug']))
        else:
            return redirect(url_for(".framework_submission_services",
                                    framework_slug=framework['slug'],
                                    lot_slug=lot_slug))
    else:
        return redirect(url_for(".view_service_submission",
                                framework_slug=framework['slug'],
                                lot_slug=draft['lotSlug'],
                                service_id=service_id,
                                delete_requested=True))


@main.route('/assets/<framework_slug>/submissions/<int:supplier_code>/<document_name>', methods=['GET'])
@login_required
def service_submission_document(framework_slug, supplier_code, document_name):
    if current_user.supplier_code != supplier_code:
        abort(404)

    uploader = s3.S3(current_app.config['DM_SUBMISSIONS_BUCKET'])
    s3_url = get_signed_document_url(uploader,
                                     "{}/submissions/{}/{}".format(framework_slug, supplier_code, document_name))
    if not s3_url:
        abort(404)

    return redirect(s3_url)


@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/<service_id>', methods=['GET'])
@login_required
def view_service_submission(framework_slug, lot_slug, service_id):
    framework, lot = get_framework_and_lot(data_api_client, framework_slug, lot_slug)

    try:
        data = data_api_client.get_draft_service(service_id)
        draft, last_edit, validation_errors = data['services'], data['auditEvents'], data['validationErrors']
    except HTTPError as e:
        abort(e.status_code)

    if draft['lotSlug'] != lot_slug or draft['frameworkSlug'] != framework_slug:
        abort(404)

    if not is_service_associated_with_supplier(draft):
        abort(404)

    content = content_loader.get_manifest(framework['slug'], 'edit_submission').filter(draft)

    sections = content.summary(draft)

    unanswered_required, unanswered_optional = count_unanswered_questions(sections)
    delete_requested = True if request.args.get('delete_requested') else False

    return render_template_with_csrf(
        "services/service_submission.html",
        framework=framework,
        lot=lot,
        confirm_remove=request.args.get("confirm_remove", None),
        service_id=service_id,
        service_data=draft,
        last_edit=last_edit,
        sections=sections,
        unanswered_required=unanswered_required,
        unanswered_optional=unanswered_optional,
        can_mark_complete=not validation_errors,
        delete_requested=delete_requested,
        declaration_status=get_declaration_status(data_api_client, framework['slug']),
        dates=content_loader.get_message(framework_slug, 'dates')
    )


@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/<service_id>/edit/<section_id>', methods=['GET'])
@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/<service_id>/edit/<section_id>/<question_slug>',
            methods=['GET'])
@login_required
def edit_service_submission(framework_slug, lot_slug, service_id, section_id, question_slug=None):
    framework, lot = get_framework_and_lot(data_api_client, framework_slug, lot_slug, allowed_statuses=['open'])

    try:
        draft = data_api_client.get_draft_service(service_id)['services']
    except HTTPError as e:
        abort(e.status_code)

    if draft['lotSlug'] != lot_slug or draft['frameworkSlug'] != framework_slug:
        abort(404)

    if not is_service_associated_with_supplier(draft):
        abort(404)

    content = content_loader.get_manifest(framework_slug, 'edit_submission').filter(draft)
    section = content.get_section(section_id)
    if section and (question_slug is not None):
        section = section.get_question_as_section(question_slug)

    if section is None or not section.editable:
        abort(404)

    draft = section.unformat_data(draft)

    return render_template_with_csrf(
        "services/edit_submission_section.html",
        section=section,
        framework=framework,
        next_section_name=get_next_section_name(content, section.id),
        service_data=draft,
        service_id=service_id,
        return_to_summary=bool(request.args.get('return_to_summary')),
        one_service_limit=lot['oneServiceLimit']
    )


@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/<service_id>/edit/<section_id>', methods=['POST'])
@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/<service_id>/edit/<section_id>/<question_slug>',
            methods=['POST'])
@login_required
def update_section_submission(framework_slug, lot_slug, service_id, section_id, question_slug=None):
    framework, lot = get_framework_and_lot(data_api_client, framework_slug, lot_slug, allowed_statuses=['open'])

    try:
        draft = data_api_client.get_draft_service(service_id)['services']
    except HTTPError as e:
        abort(e.status_code)

    if draft['lotSlug'] != lot_slug or draft['frameworkSlug'] != framework_slug:
        abort(404)

    if not is_service_associated_with_supplier(draft):
        abort(404)

    content = content_loader.get_manifest(framework_slug, 'edit_submission').filter(draft)
    section = content.get_section(section_id)
    if section and (question_slug is not None):
        section = section.get_question_as_section(question_slug)

    if section is None or not section.editable:
        abort(404)

    errors = None
    document_errors = None
    update_data = section.get_data(request.form)

    uploader = s3.S3(current_app.config['DM_SUBMISSIONS_BUCKET'])
    documents_url = url_for('.dashboard', _external=True) + '/assets/'
    uploaded_documents, document_errors = upload_service_documents(
        uploader, documents_url, draft, request.files, section,
        public=False)

    if document_errors:
        errors = section.get_error_messages(document_errors)
    else:
        update_data.update(uploaded_documents)

    if not errors and section.has_changes_to_save(draft, update_data):
        try:
            data_api_client.update_draft_service(
                service_id,
                update_data,
                current_user.email_address,
                page_questions=section.get_field_names()
            )
        except HTTPError as e:
            update_data = section.unformat_data(update_data)
            errors = section.get_error_messages(e.message)

    if errors:
        keys_required_for_template = ['serviceName', 'lot', 'lotName']
        for k in keys_required_for_template:
            if k in draft and k not in update_data:
                update_data[k] = draft[k]
        return render_template_with_csrf(
            "services/edit_submission_section.html",
            framework=framework,
            section=section,
            next_section_name=get_next_section_name(content, section.id),
            service_data=update_data,
            service_id=service_id,
            one_service_limit=lot['oneServiceLimit'],
            return_to_summary=bool(request.args.get('return_to_summary')),
            errors=errors
        )

    return_to_summary = bool(request.args.get('return_to_summary'))
    next_section = content.get_next_editable_section_id(section_id)

    if next_section and not return_to_summary and request.form.get('continue_to_next_section'):
        return redirect(url_for(".edit_service_submission",
                                framework_slug=framework['slug'],
                                lot_slug=draft['lotSlug'],
                                service_id=service_id,
                                section_id=next_section))
    else:
        return redirect(url_for(".view_service_submission",
                                framework_slug=framework['slug'],
                                lot_slug=draft['lotSlug'],
                                service_id=service_id,
                                _anchor=section_id))


@main.route('/frameworks/<framework_slug>/submissions/<lot_slug>/<service_id>/remove/<section_id>/<question_slug>',
            methods=['GET', 'POST'])
@login_required
def remove_subsection(framework_slug, lot_slug, service_id, section_id, question_slug):
    try:
        draft = data_api_client.get_draft_service(service_id)['services']
    except HTTPError as e:
        abort(e.status_code)

    if not is_service_associated_with_supplier(draft):
        abort(404)

    content = content_loader.get_manifest(framework_slug, 'edit_submission').filter(draft)
    section = content.get_section(section_id)
    containing_section = section
    if section and (question_slug is not None):
        section = section.get_question_as_section(question_slug)
    if section is None or not section.editable:
        abort(404)

    question_to_remove = content.get_question_by_slug(question_slug)
    fields_to_remove = question_to_remove.form_fields

    if request.args.get("confirm") and request.method == "POST":
        # Remove the section
        update_json = {field: None for field in fields_to_remove}
        try:
            data_api_client.update_draft_service(
                service_id,
                update_json,
                current_user.email_address
            )
            flash({'service_name': question_to_remove.label}, 'service_deleted')
        except HTTPError as e:
            if e.status_code == 400:
                # You can't remove the last one
                flash({
                    'remove_last_attempted': containing_section.name,
                    'service_name': question_to_remove.label
                }, 'error')
            else:
                abort(e.status_code)

    else:
        section_responses = [field for field in containing_section.get_field_names() if field in draft]
        fields_remaining_after_removal = [field for field in section_responses if field not in fields_to_remove]

        if draft['status'] == 'not-submitted' or len(fields_remaining_after_removal) > 0:
            # Show page with "Are you sure?" message and button
            return redirect(
                url_for('.view_service_submission',
                        framework_slug=framework_slug,
                        lot_slug=lot_slug,
                        service_id=service_id,
                        confirm_remove=question_slug,
                        section_id=section_id
                        )
            )
        else:
            # You can't remove the last one
            flash({
                'remove_last_attempted': containing_section.name,
                'service_name': question_to_remove.label,
                'virtual_pageview_url': "{}/{}".format(
                    url_for(".remove_subsection",
                            framework_slug=framework_slug,
                            lot_slug=lot_slug,
                            service_id=service_id,
                            section_id=section_id,
                            question_slug=question_slug
                            ),
                    "remove-last-subsection-attempt"
                )
            }, 'error')

    return redirect(
        url_for('.view_service_submission',
                framework_slug=framework_slug,
                lot_slug=lot_slug,
                service_id=service_id,
                confirm_remove=None
                ))
