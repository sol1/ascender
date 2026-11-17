# Copyright (c) 2017 Ansible, Inc.
# All Rights Reserved.

from django.urls import path

from ascender.api.views.webhooks import NetBoxWebhookReceiver, WebhookKeyView
from ascender.api.views.inventory_source import (
    InventorySourceList,
    InventorySourceDetail,
    InventorySourceUpdateView,
    InventorySourceUpdatesList,
    InventorySourceActivityStreamList,
    InventorySourceSchedulesList,
    InventorySourceCredentialsList,
    InventorySourceGroupsList,
    InventorySourceInstanceGroupsList,
    InventorySourceHostsList,
    InventorySourceNotificationTemplatesErrorList,
    InventorySourceNotificationTemplatesStartedList,
    InventorySourceNotificationTemplatesSuccessList,
)

urls = [
    path('', InventorySourceList.as_view(), name='inventory_source_list'),
    path('<int:pk>/', InventorySourceDetail.as_view(), name='inventory_source_detail'),
    path('<int:pk>/update/', InventorySourceUpdateView.as_view(), name='inventory_source_update_view'),
    # Only the NetBox receiver, not the whole webhooks urlconf: including that
    # would also advertise github/, gitlab/ and bitbucket_dc/ endpoints on every
    # inventory source, which no inventory source can ever accept.
    path('<int:pk>/webhook_key/', WebhookKeyView.as_view(), {'model_kwarg': 'inventory_sources'}, name='inventory_source_webhook_key'),
    path('<int:pk>/netbox/', NetBoxWebhookReceiver.as_view(), {'model_kwarg': 'inventory_sources'}, name='webhook_receiver_netbox'),
    path('<int:pk>/inventory_updates/', InventorySourceUpdatesList.as_view(), name='inventory_source_updates_list'),
    path('<int:pk>/activity_stream/', InventorySourceActivityStreamList.as_view(), name='inventory_source_activity_stream_list'),
    path('<int:pk>/schedules/', InventorySourceSchedulesList.as_view(), name='inventory_source_schedules_list'),
    path('<int:pk>/credentials/', InventorySourceCredentialsList.as_view(), name='inventory_source_credentials_list'),
    path('<int:pk>/instance_groups/', InventorySourceInstanceGroupsList.as_view(), name='inventory_source_instance_groups_list'),
    path('<int:pk>/groups/', InventorySourceGroupsList.as_view(), name='inventory_source_groups_list'),
    path('<int:pk>/hosts/', InventorySourceHostsList.as_view(), name='inventory_source_hosts_list'),
    path(
        '<int:pk>/notification_templates_started/',
        InventorySourceNotificationTemplatesStartedList.as_view(),
        name='inventory_source_notification_templates_started_list',
    ),
    path(
        '<int:pk>/notification_templates_error/',
        InventorySourceNotificationTemplatesErrorList.as_view(),
        name='inventory_source_notification_templates_error_list',
    ),
    path(
        '<int:pk>/notification_templates_success/',
        InventorySourceNotificationTemplatesSuccessList.as_view(),
        name='inventory_source_notification_templates_success_list',
    ),
]

__all__ = ['urls']
