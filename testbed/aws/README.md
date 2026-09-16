# AWS testbed

A small AWS environment used to test EasyMigrate end to end.

    export TF_VAR_db_password='<choose a password>'
    terraform -chdir=testbed/aws init
    terraform -chdir=testbed/aws apply
    # ... run the EasyMigrate tests ...
    terraform -chdir=testbed/aws destroy

**Cost:** EKS, RDS and the NAT gateway are billed by the hour, plus its Elastic IP and data processing charges. Create the testbed only while testing and destroy it straight afterwards.
