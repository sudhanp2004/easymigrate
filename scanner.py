import boto3
import json

def scan_aws_environment():
    region = "ap-south-1"
    ec2_client = boto3.client('ec2', region_name=region)
    rds_client = boto3.client('rds', region_name=region)
    eks_client = boto3.client('eks', region_name=region)

    print("Scanning AWS infrastructure (VPCs, Subnets, RDS, EKS)...")

    # 1. Fetch VPCs
    vpcs_response = ec2_client.describe_vpcs(
        Filters=[{'Name': 'tag:Name', 'Values': ['migration-test-vpc']}]
    )
    
    # 2. Fetch Subnets
    subnets_response = ec2_client.describe_subnets(
        Filters=[{'Name': 'tag:Name', 'Values': ['migration-test-subnet-*']}]
    )

    # 3. Fetch RDS Database Instances
    rds_response = rds_client.describe_db_instances(
        DBInstanceIdentifier='test-postgres-db'
    )

    # 4. Fetch EKS Clusters and Node Groups
    clusters = eks_client.list_clusters().get('Clusters', [])
    eks_clusters_data = []
    
    for cluster_name in clusters:
        cluster_info = eks_client.describe_cluster(name=cluster_name)['cluster']
        
        # Fetch node groups for this cluster
        nodegroups = eks_client.list_nodegroups(clusterName=cluster_name).get('nodegroups', [])
        ng_details = []
        for ng in nodegroups:
            ng_info = eks_client.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng)['nodegroup']
            ng_details.append(ng_info)
            
        cluster_info['NodeGroups'] = ng_details
        eks_clusters_data.append(cluster_info)

    # Assemble into a clean dictionary structure
    infra_state = {
        "region": region,
        "vpcs": vpcs_response.get('Vpcs', []),
        "subnets": subnets_response.get('Subnets', []),
        "rds_instances": rds_response.get('DBInstances', []),
        "eks_clusters": eks_clusters_data
    }

    # Save to local JSON file
    output_file = "aws_state.json"
    with open(output_file, "w") as f:
        json.dump(infra_state, f, indent=4, default=str)

    print(f"Scan complete! Updated infrastructure state saved to {output_file}")

if __name__ == "__main__":
    scan_aws_environment()