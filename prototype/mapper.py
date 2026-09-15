import json

def transform_to_ir():
    with open("aws_state.json", "r") as f:
        aws_data = json.load(f)

    print("Mapping raw AWS state (including EKS) to Cloud-Agnostic IR...")

    ir_vpcs = [{
        "id": vpc["VpcId"],
        "cidr": vpc["CidrBlock"],
        "name": next((tag["Value"] for tag in vpc.get("Tags", []) if tag["Key"] == "Name"), "default-vpc")
    } for vpc in aws_data.get("vpcs", [])]

    ir_subnets = [{
        "id": subnet["SubnetId"],
        "vpc_id": subnet["VpcId"],
        "cidr": subnet["CidrBlock"],
        "zone": subnet["AvailabilityZone"],
        "name": next((tag["Value"] for tag in subnet.get("Tags", []) if tag["Key"] == "Name"), "default-subnet")
    } for subnet in aws_data.get("subnets", [])]

    ir_databases = [{
        "identifier": db["DBInstanceIdentifier"],
        "engine": db["Engine"],
        "version": db["EngineVersion"],
        "instance_class": db["DBInstanceClass"],
        "storage_gb": db["AllocatedStorage"],
        "db_name": db["DBName"],
        "master_user": db["MasterUsername"],
        "subnet_group": db["DBSubnetGroup"]["DBSubnetGroupName"]
    } for db in aws_data.get("rds_instances", [])]

    ir_eks = []
    for cluster in aws_data.get("eks_clusters", []):
        node_groups = []
        for ng in cluster.get("NodeGroups", []):
            node_groups.append({
                "name": ng["nodegroupName"],
                "instance_types": ng.get("instanceTypes", ["t3.micro"]),
                "desired_size": ng["scalingConfig"]["desiredSize"]
            })
        ir_eks.append({
            "name": cluster["name"],
            "version": cluster.get("version"),
            "node_groups": node_groups
        })

    intermediate_representation = {
        "source_cloud": "aws",
        "target_cloud": "gcp",
        "region": aws_data.get("region"),
        "network": {
            "vpcs": ir_vpcs,
            "subnets": ir_subnets
        },
        "databases": ir_databases,
        "kubernetes_clusters": ir_eks
    }

    with open("migration_ir.json", "w") as f:
        json.dump(intermediate_representation, f, indent=4)

    print("IR Mapping complete with EKS support!")

if __name__ == "__main__":
    transform_to_ir()