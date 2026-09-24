KYC_REQUIREMENTS = {
    'standard': {
        'description': 'Default merchant onboarding risk level.',
        'required_documents': [
            'company_registration_certificate',
            'tax_registration_document',
            'director_identity_document',
            'beneficial_owner_declaration',
            'merchant_website_or_business_description',
        ],
        'review_interval_days': 365,
    },
    'high': {
        'description': 'Enhanced due diligence for higher risk merchants.',
        'required_documents': [
            'company_registration_certificate',
            'tax_registration_document',
            'director_identity_document',
            'beneficial_owner_declaration',
            'merchant_website_or_business_description',
            'source_of_funds_statement',
            'bank_account_ownership_proof',
            'enhanced_compliance_questionnaire',
        ],
        'review_interval_days': 180,
    },
    'prohibited': {
        'description': 'Merchant must not be processed without executive/legal approval.',
        'required_documents': [],
        'review_interval_days': 0,
    },
}


AML_RISK_MATRIX = {
    'allow': {
        'score_range': '0-49',
        'action': 'Process automatically and keep audit trail.',
    },
    'review': {
        'score_range': '50-89',
        'action': 'Manual support/admin review before final settlement decision.',
    },
    'deny': {
        'score_range': '90-100',
        'action': 'Reject operation, log reason, and assess blacklist/escalation.',
    },
}


COMPLIANCE_POLICY = {
    'kyc_status_flow': {
        'not_started': ['pending'],
        'pending': ['approved', 'rejected'],
        'approved': ['pending', 'rejected'],
        'rejected': ['pending'],
    },
    'risk_levels': list(KYC_REQUIREMENTS.keys()),
    'kyc_requirements': KYC_REQUIREMENTS,
    'aml_risk_matrix': AML_RISK_MATRIX,
    'blacklist_kinds': ['ip', 'merchant', 'card', 'phone', 'requisite'],
    'mandatory_controls': [
        'Merchant legal identity must be reviewed before production traffic.',
        'Beneficial ownership must be collected for real merchants.',
        'High-risk merchants require enhanced due diligence.',
        'All blacklist hits must be logged and reviewed.',
        'All risk decisions must remain exportable through audit/reporting.',
        'Compliance rules must be approved by legal/compliance owner before production launch.',
    ],
}
