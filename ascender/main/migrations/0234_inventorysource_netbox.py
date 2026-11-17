from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('main', '0233_job_monitor_lease'),
    ]

    operations = [
        migrations.AlterField(
            model_name='inventorysource',
            name='source',
            field=models.CharField(
                choices=[
                    ('file', 'File, Directory or Script'),
                    ('constructed', 'Template additional groups and hostvars at runtime'),
                    ('scm', 'Sourced from a Project'),
                    ('ec2', 'Amazon EC2'),
                    ('gce', 'Google Compute Engine'),
                    ('azure_rm', 'Microsoft Azure Resource Manager'),
                    ('vmware', 'VMware vCenter'),
                    ('satellite6', 'Red Hat Satellite 6'),
                    ('openstack', 'OpenStack'),
                    ('terraform', 'Terraform State'),
                    ('netbox', 'NetBox'),
                    ('ascender', 'CIQ Ascender Automation Platform'),
                ],
                default=None,
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name='inventoryupdate',
            name='source',
            field=models.CharField(
                choices=[
                    ('file', 'File, Directory or Script'),
                    ('constructed', 'Template additional groups and hostvars at runtime'),
                    ('scm', 'Sourced from a Project'),
                    ('ec2', 'Amazon EC2'),
                    ('gce', 'Google Compute Engine'),
                    ('azure_rm', 'Microsoft Azure Resource Manager'),
                    ('vmware', 'VMware vCenter'),
                    ('satellite6', 'Red Hat Satellite 6'),
                    ('openstack', 'OpenStack'),
                    ('terraform', 'Terraform State'),
                    ('netbox', 'NetBox'),
                    ('ascender', 'CIQ Ascender Automation Platform'),
                ],
                default=None,
                max_length=32,
            ),
        ),
    ]
