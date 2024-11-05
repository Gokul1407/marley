# -*- coding: utf-8 -*-
# Copyright (c) 2015, ESS LLP and contributors
# For license information, please see license.txt

import datetime
import json

import frappe
from frappe.model.document import Document
from frappe.utils import getdate


class FeeValidity(Document):
	def validate(self):
		self.update_status()

	def update_status(self):
		if getdate(self.valid_till) < getdate():
			self.status = "Expired"
		elif self.visited == self.max_visits:
			self.status = "Completed"
		else:
			self.status = "Active"


def is_free_follow_up_enabled(practitioner, department):
	if practitioner:
		enable_free_follow_up = frappe.get_cached_value(
			"Healthcare Practitioner", practitioner, "enable_free_follow_ups"
		)
		if enable_free_follow_up:
			return True

	if department:
		enable_free_follow_up = frappe.get_cached_value(
			"Medical Department", department, "enable_free_follow_ups"
		)
		if enable_free_follow_up:
			return True

	return frappe.db.get_single_value("Healthcare Settings", "enable_free_follow_ups")


def create_fee_validity(appointment):
	if patient_has_validity(appointment):
		return

	practitioner_doc = frappe.get_doc("Healthcare Practitioner", appointment.practitioner)
	department_doc = frappe.get_doc("Medical Department", appointment.department)
	settings = frappe.get_single("Healthcare Settings")

	enable_free_follow_up = is_free_follow_up_enabled(
		appointment.practitioner, appointment.department
	)

	if enable_free_follow_up:
		max_visits = practitioner_doc.max_visits or department_doc.max_visits or settings.max_visits or 1
		valid_days = practitioner_doc.valid_days or department_doc.valid_days or settings.valid_days or 1
	else:
		max_visits = 1
		valid_days = 1

	fee_validity = frappe.new_doc("Fee Validity")
	fee_validity.practitioner = appointment.practitioner
	fee_validity.patient = appointment.patient
	fee_validity.medical_department = appointment.department
	fee_validity.patient_appointment = appointment.name
	fee_validity.sales_invoice_ref = frappe.db.get_value(
		"Sales Invoice Item", {"reference_dn": appointment.name}, "parent"
	)
	fee_validity.max_visits = max_visits
	valid_days = valid_days
	fee_validity.visited = 0
	fee_validity.start_date = getdate(appointment.appointment_date)
	fee_validity.valid_till = getdate(appointment.appointment_date) + datetime.timedelta(
		days=int(valid_days)
	)
	fee_validity.save(ignore_permissions=True)
	return fee_validity


def patient_has_validity(appointment):
	validity_exists = frappe.db.exists(
		"Fee Validity",
		{
			"practitioner": appointment.practitioner,
			"patient": appointment.patient,
			"status": "Active",
			"valid_till": [">=", appointment.appointment_date],
			"start_date": ["<=", appointment.appointment_date],
		},
	)

	return validity_exists


@frappe.whitelist()
def check_fee_validity(appointment, date=None, practitioner=None):
	if isinstance(appointment, str):
		appointment = json.loads(appointment)
		appointment = frappe.get_doc(appointment)

	enable_free_follow_up = is_free_follow_up_enabled(
		appointment.practitioner, appointment.department
	)
	if not enable_free_follow_up:
		return

	date = getdate(date) if date else appointment.appointment_date

	filters = {
		"practitioner": practitioner if practitioner else appointment.practitioner,
		"patient": appointment.patient,
		"valid_till": (">=", date),
		"start_date": ("<=", date),
	}
	if appointment.status != "Cancelled":
		filters["status"] = "Active"
	else:
		filters["patient_appointment"] = appointment.name

	validity = frappe.db.exists(
		"Fee Validity",
		filters,
	)

	if not validity:
		# return valid fee validity when rescheduling appointment
		if appointment.get("__islocal"):
			return
		else:
			validity = get_fee_validity(appointment.get("name"), date) or None
			if validity and len(validity):
				return frappe.get_doc("Fee Validity", validity[0].get("name"))
		return

	validity = frappe.get_doc("Fee Validity", validity)
	return validity


def manage_fee_validity(appointment):
	enable_free_follow_up = is_free_follow_up_enabled(
		appointment.practitioner, appointment.department
	)
	# Update fee validity dates when rescheduling an invoiced appointment
	if enable_free_follow_up:
		invoiced_fee_validity = frappe.db.exists(
			"Fee Validity", {"patient_appointment": appointment.name}
		)
		if invoiced_fee_validity and appointment.invoiced:
			start_date = frappe.db.get_value("Fee Validity", invoiced_fee_validity, "start_date")
			if getdate(appointment.appointment_date) != start_date:
				frappe.db.set_value(
					"Fee Validity",
					invoiced_fee_validity,
					{
						"start_date": appointment.appointment_date,
						"valid_till": getdate(appointment.appointment_date)
						+ datetime.timedelta(
							days=int(frappe.db.get_single_value("Healthcare Settings", "valid_days") or 1)
						),
					},
				)

	fee_validity = check_fee_validity(appointment)

	if fee_validity:
		exists = frappe.db.exists("Fee Validity Reference", {"appointment": appointment.name})
		if appointment.status == "Cancelled" and fee_validity.visited > 0:
			fee_validity.visited -= 1
			frappe.db.delete("Fee Validity Reference", {"appointment": appointment.name})
		elif fee_validity.status != "Active":
			return
		elif appointment.name != fee_validity.patient_appointment and not exists:
			fee_validity.visited += 1
			fee_validity.append("ref_appointments", {"appointment": appointment.name})
		fee_validity.save(ignore_permissions=True)
	else:
		# remove appointment from fee validity reference when rescheduling an appointment to date not in fee validity
		free_visit_validity = frappe.db.get_value(
			"Fee Validity Reference", {"appointment": appointment.name}, "parent"
		)
		if free_visit_validity:
			fee_validity = frappe.get_doc(
				"Fee Validity",
				free_visit_validity,
			)
			if fee_validity:
				frappe.db.delete("Fee Validity Reference", {"appointment": appointment.name})
				if fee_validity.visited > 0:
					fee_validity.visited -= 1
					fee_validity.save(ignore_permissions=True)
		fee_validity = create_fee_validity(appointment)
	return fee_validity


@frappe.whitelist()
def get_fee_validity(appointment_name, date, ignore_status=False):
	"""
	Get the fee validity details for the free visit appointment
	:params appointment_name: Appointment doc name
	:params date: Schedule date
	:params ignore_status: status will not filter in query
	:return fee validity name and valid_till values of free visit appointments
	"""
	if appointment_name:
		appointment_doc = frappe.get_doc("Patient Appointment", appointment_name)
	fee_validity = frappe.qb.DocType("Fee Validity")
	child = frappe.qb.DocType("Fee Validity Reference")

	query = (
		frappe.qb.from_(fee_validity)
		.inner_join(child)
		.on(fee_validity.name == child.parent)
		.select(fee_validity.name, fee_validity.valid_till)
		.where(fee_validity.start_date <= date)
		.where(fee_validity.valid_till >= date)
		.where(fee_validity.patient == appointment_doc.patient)
		.where(fee_validity.practitioner == appointment_doc.practitioner)
		.where(child.appointment == appointment_name)
	)

	if not ignore_status:
		query = query.where(fee_validity.status == "Active")

	return query.run(as_dict=True)


def update_validity_status():
	# update the status of fee validity daily
	validities = frappe.db.get_all("Fee Validity", {"status": ["not in", ["Expired", "Cancelled"]]})

	for fee_validity in validities:
		fee_validity_doc = frappe.get_doc("Fee Validity", fee_validity.name)
		fee_validity_doc.update_status()
		fee_validity_doc.save()
