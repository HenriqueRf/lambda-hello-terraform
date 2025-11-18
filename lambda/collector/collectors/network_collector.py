import json
import logging
from .base import ResourceCollector, format_aws_datetime

logger = logging.getLogger()

def to_dynamodb_format(data: dict) -> dict:
    """
    Convert a dictionary with raw values to DynamoDB-compatible format.
    Keeps lists as lists and dicts as JSON strings.
    """
    dynamodb_item = {}
    for key, value in data.items():
        if isinstance(value, bool):
            dynamodb_item[key] = value
        elif isinstance(value, (int, float)):
            dynamodb_item[key] = value
        elif isinstance(value, list):
            dynamodb_item[key] = value
        elif isinstance(value, dict):
            dynamodb_item[key] = json.dumps(value)
        elif value is None:
            dynamodb_item[key] = None
        else:
            dynamodb_item[key] = str(value)
    return dynamodb_item

class NetworkCollector(ResourceCollector):
    """
    Collects networking and VPC resources including security analysis.
    Resources: VPCs, Security Groups, Subnets, NAT Gateways, Internet Gateways,
    Load Balancers, Auto Scaling Groups, Network ACLs, Route Tables, 
    Transit Gateways, Direct Connect, VPN Connections, VPC Endpoints, and more.
    """
    
    def collect(self):
        """Main method to collect all networking resources."""
        logger.info(f"Starting Networking collection for account {self.account_id} in region {self.region}")
        total_collected_count = 0
        try:
            ec2 = self.get_client('ec2')
            elbv2 = self.get_client('elbv2')
            elb = self.get_client('elb')
            autoscaling = self.get_client('autoscaling')
            directconnect = self.get_client('directconnect')

            total_collected_count += self._collect_vpcs(ec2)
            total_collected_count += self._collect_security_groups(ec2)
            total_collected_count += self._collect_subnets(ec2)
            total_collected_count += self._collect_nat_gateways(ec2)
            total_collected_count += self._collect_internet_gateways(ec2)
            total_collected_count += self._collect_elastic_ips(ec2)
            total_collected_count += self._collect_route_tables(ec2)
            total_collected_count += self._collect_network_acls(ec2)
            total_collected_count += self._collect_vpc_endpoints(ec2)
            total_collected_count += self._collect_vpc_peering_connections(ec2)
            total_collected_count += self._collect_vpn_connections(ec2)
            total_collected_count += self._collect_transit_gateways(ec2)
            total_collected_count += self._collect_transit_gateway_attachments(ec2)
            total_collected_count += self._collect_application_load_balancers(elbv2)
            total_collected_count += self._collect_classic_load_balancers(elb)
            total_collected_count += self._collect_auto_scaling_groups(autoscaling)
            total_collected_count += self._collect_direct_connect_connections(directconnect)
            total_collected_count += self._collect_direct_connect_virtual_interfaces(directconnect)

            logger.info(f"Finished Networking collection for account {self.account_id} in region {self.region}. "
                        f"Added {total_collected_count} resources to the item list.")
            return self.items
        except Exception as e:
            logger.error(f"Critical error during Networking collection in region {self.region}: {str(e)}", exc_info=True)
            return []
        
    def _collect_vpcs(self, ec2):
        """Collect VPC information."""
        vpc_count = 0
        try:
            response = ec2.describe_vpcs()
            for vpc in response.get('Vpcs', []):
                tags = vpc.get('Tags', [])
                tags_json = json.dumps(tags) if tags else '[]'
                vpc_name = next((t.get('Value') for t in tags if t.get('Key') == 'Name'), 'N/A')
                item_data = {
                    'vpcId': vpc['VpcId'],
                    'vpcName': vpc_name,
                    'cidrBlock': vpc.get('CidrBlock', 'N/A'),
                    'state': vpc.get('State', 'N/A'),
                    'isDefault': vpc.get('IsDefault', False),
                    'enableDnsHostnames': vpc.get('EnableDnsHostnames', False),
                    'enableDnsSupport': vpc.get('EnableDnsSupport', True),
                    'flowLogsEnabled': False,
                    'instanceTenancy': vpc.get('InstanceTenancy', 'default'),
                    'tags': tags_json
                }
                self.add_item('VPC', vpc['VpcId'], to_dynamodb_format(item_data))
                vpc_count += 1
            return vpc_count
        except Exception as e:
            logger.error(f"Error collecting VPCs: {str(e)}", exc_info=True)
            return 0
  
    def _collect_security_groups(self, ec2):
        """Collect Security Group information with complete security analysis."""
        sg_count = 0
        try:
            paginator = ec2.get_paginator('describe_security_groups')
            for page in paginator.paginate():
                for sg in page.get('SecurityGroups', []):
                    tags = sg.get('Tags', [])
                    tags_json = json.dumps(tags) if tags else '[]'
                    sg_name_tag = next((t.get('Value') for t in tags if t.get('Key') == 'Name'), 'N/A')
                    
                    # Get the complete rules for analysis
                    ingress_rules = sg.get('IpPermissions', [])
                    egress_rules = sg.get('IpPermissionsEgress', [])
                    
                    # Analyze ingress rules for exposed ports and risky configurations
                    ingress_analysis = self._analyze_security_group_rules(ingress_rules, 'ingress')
                    
                    # Analyze egress rules (typically less critical but still important)
                    egress_analysis = self._analyze_security_group_rules(egress_rules, 'egress')
                    
                    # Convert rules to JSON strings for DynamoDB storage
                    ingress_rules_json = json.dumps(ingress_rules) if ingress_rules else '[]'
                    egress_rules_json = json.dumps(egress_rules) if egress_rules else '[]'
                    
                    item_data = {
                        'groupId': sg['GroupId'],
                        'groupName': sg.get('GroupName', 'N/A'),
                        'groupNameTag': sg_name_tag,
                        'description': sg.get('Description', ''),
                        'vpcId': sg.get('VpcId', 'N/A'),
                        'tags': tags_json,
                        
                        # Original counts
                        'ingressRuleCount': len(ingress_rules),
                        'egressRuleCount': len(egress_rules),
                        
                        # Complete rules (stored as JSON strings)
                        'ingressRules': ingress_rules_json,
                        'egressRules': egress_rules_json,
                        
                        # Ingress analysis
                        'hasExposedIngressPorts': ingress_analysis['hasExposedPorts'],
                        'exposedIngressPorts': ingress_analysis['exposedPorts'],
                        'allIngressPortsExposed': ingress_analysis['allPortsExposed'],
                        'riskyIngressRules': ingress_analysis['riskyRules'],
                        
                        # Egress analysis
                        'hasExposedEgressPorts': egress_analysis['hasExposedPorts'],
                        'exposedEgressPorts': egress_analysis['exposedPorts'],
                        'allEgressPortsExposed': egress_analysis['allPortsExposed'],
                        'riskyEgressRules': egress_analysis['riskyRules'],
                        
                        # Combined flag for backward compatibility (used by metrics_calculator)
                        'hasExposedIngressPorts': ingress_analysis['hasExposedPorts']
                    }
                    
                    self.add_item('SecurityGroup', sg['GroupId'], to_dynamodb_format(item_data))
                    sg_count += 1
            return sg_count
        except Exception as e:
            logger.error(f"Error collecting Security Groups: {str(e)}", exc_info=True)
            return 0
                    
    def _analyze_security_group_rules(self, rules, rule_type='ingress'):
        """Analyze security group rules for exposed ports and risky configurations."""
        exposed_ports = []
        risky_rules = []
        all_ports_exposed = False
        
        for rule in rules:
            is_exposed_ipv4 = any(
                ip_range.get('CidrIp') == '0.0.0.0/0' 
                for ip_range in rule.get('IpRanges', [])
            )
            is_exposed_ipv6 = any(
                ip_range.get('CidrIpv6') == '::/0' 
                for ip_range in rule.get('Ipv6Ranges', [])
            )
            
            if is_exposed_ipv4 or is_exposed_ipv6:
                from_port = rule.get('FromPort', -1)
                to_port = rule.get('ToPort', -1)
                protocol = rule.get('IpProtocol', 'all')
                
                # Check if all traffic is allowed
                if protocol == '-1' or (from_port == 0 and to_port == 65535):
                    all_ports_exposed = True
                    risky_rules.append({
                        'type': 'all_ports_exposed',
                        'protocol': protocol,
                        'source': '0.0.0.0/0' if is_exposed_ipv4 else '::/0'
                    })
                elif from_port != -1:
                    # Define risky ports to check
                    risky_ports = {
                        22: 'SSH',
                        3389: 'RDP',
                        1433: 'MSSQL',
                        3306: 'MySQL',
                        5432: 'PostgreSQL',
                        27017: 'MongoDB',
                        6379: 'Redis',
                        9200: 'Elasticsearch',
                        5984: 'CouchDB',
                        11211: 'Memcached',
                        445: 'SMB',
                        135: 'RPC',
                        139: 'NetBIOS',
                        21: 'FTP',
                        23: 'Telnet',
                        25: 'SMTP',
                        110: 'POP3',
                        143: 'IMAP',
                        161: 'SNMP',
                        1521: 'Oracle',
                        2049: 'NFS',
                        5900: 'VNC',
                        8080: 'HTTP-Alt',
                        8443: 'HTTPS-Alt'
                    }
                    
                    # Check each port in the range
                    for port in range(from_port, to_port + 1):
                        if port in risky_ports:
                            exposed_ports.append(port)
                            risky_rules.append({
                                'type': 'risky_port_exposed',
                                'port': port,
                                'service': risky_ports[port],
                                'protocol': protocol,
                                'source': '0.0.0.0/0' if is_exposed_ipv4 else '::/0'
                            })
                        elif from_port == to_port:
                            # Single port that's not in risky list but still exposed
                            exposed_ports.append(port)
                            if port == 80:
                                risky_rules.append({
                                    'type': 'web_port_exposed',
                                    'port': port,
                                    'service': 'HTTP',
                                    'protocol': protocol,
                                    'source': '0.0.0.0/0' if is_exposed_ipv4 else '::/0'
                                })
                            elif port == 443:
                                risky_rules.append({
                                    'type': 'web_port_exposed',
                                    'port': port,
                                    'service': 'HTTPS',
                                    'protocol': protocol,
                                    'source': '0.0.0.0/0' if is_exposed_ipv4 else '::/0'
                                })
        
        return {
            'hasExposedPorts': len(exposed_ports) > 0,
            'exposedPorts': exposed_ports,
            'allPortsExposed': all_ports_exposed,
            'riskyRules': risky_rules,
            'ruleCount': len(rules)
        }
    
    def _collect_subnets(self, ec2):
        """Collect Subnet information."""
        subnet_count = 0
        try:
            paginator = ec2.get_paginator('describe_subnets')
            for page in paginator.paginate():
                for subnet in page.get('Subnets', []):
                    tags = subnet.get('Tags', [])
                    tags_json = json.dumps(tags) if tags else '[]'
                    subnet_name = next((t.get('Value') for t in tags if t.get('Key') == 'Name'), 'N/A')
                    item_data = {
                        'subnetId': subnet['SubnetId'],
                        'subnetName': subnet_name,
                        'vpcId': subnet.get('VpcId', 'N/A'),
                        'cidrBlock': subnet.get('CidrBlock', 'N/A'),
                        'availabilityZone': subnet.get('AvailabilityZone', 'N/A'),
                        'availabilityZoneId': subnet.get('AvailabilityZoneId', 'N/A'),
                        'state': subnet.get('State', 'N/A'),
                        'availableIpAddressCount': subnet.get('AvailableIpAddressCount', 0),
                        'tags': tags_json
                    }
                    self.add_item('Subnet', subnet['SubnetId'], to_dynamodb_format(item_data))
                    subnet_count += 1
            return subnet_count
        except Exception as e:
            logger.error(f"Error collecting Subnets: {str(e)}", exc_info=True)
            return 0
    
    def _collect_nat_gateways(self, ec2):
        """Collect NAT Gateway information."""
        nat_count = 0
        try:
            paginator = ec2.get_paginator('describe_nat_gateways')
            for page in paginator.paginate():
                for nat in page.get('NatGateways', []):
                    nat_id = nat['NatGatewayId']
                    tags = nat.get('Tags', [])
                    tags_json = json.dumps(tags) if tags else '[]'

                    nat_name = next(
                        (t.get('Value') for t in tags if t.get('Key') == 'Name'),
                        'N/A'
                    )

                    create_time = nat.get('CreateTime')
                    formatted_create_time = format_aws_datetime(create_time) if create_time else None

                    public_ips = [
                        addr['PublicIp']
                        for addr in nat.get('NatGatewayAddresses', [])
                        if addr.get('PublicIp')
                    ]

                    item_data = {
                        'natGatewayId': nat_id,
                        'natGatewayName': nat_name,
                        'state': nat.get('State', 'N/A'),
                        'vpcId': nat.get('VpcId', 'N/A'),
                        'subnetId': nat.get('SubnetId', 'N/A'),
                        'connectivityType': nat.get('ConnectivityType', 'public'),
                        'createdAt': formatted_create_time,
                        'tags': tags_json,
                        'publicIps': public_ips
                    }

                    self.add_item('NATGateway', nat_id, to_dynamodb_format(item_data))
                    nat_count += 1

            if nat_count > 0:
                logger.debug(f"Added {nat_count} NAT Gateways from region {self.region} to the item list.")
            return nat_count
        except Exception as e:
            logger.error(f"Error collecting NAT Gateways in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_internet_gateways(self, ec2):
        """Collect Internet Gateway information."""
        igw_count = 0
        try:
            response = ec2.describe_internet_gateways()
            for igw in response.get('InternetGateways', []):
                igw_id = igw['InternetGatewayId']
                tags = igw.get('Tags', [])
                tags_json = json.dumps(tags) if tags else '[]'

                igw_name = next(
                    (t.get('Value') for t in tags if t.get('Key') == 'Name'),
                    'N/A'
                )

                attached_vpcs = [att['VpcId'] for att in igw.get('Attachments', [])]

                item_data = {
                    'internetGatewayId': igw_id,
                    'internetGatewayName': igw_name,
                    'attachmentCount': len(attached_vpcs),
                    'attachedVpcs': attached_vpcs,  # now a list instead of flattened keys
                    'tags': tags_json
                }

                self.add_item('InternetGateway', igw_id, to_dynamodb_format(item_data))
                igw_count += 1

            if igw_count > 0:
                logger.debug(f"Added {igw_count} Internet Gateways from region {self.region} to the item list.")
            return igw_count
        except Exception as e:
            logger.error(f"Error collecting Internet Gateways in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_elastic_ips(self, ec2):
        """Collect Elastic IP information (moved from EC2Collector)."""
        eip_count = 0
        try:
            addresses_response = ec2.describe_addresses()
            for address in addresses_response.get('Addresses', []):
                eip_id = address.get('AllocationId', address.get('PublicIp'))
                if not eip_id:
                    logger.warning(f"Skipping EIP with no AllocationId or PublicIp: {address}")
                    continue
                
                tags = address.get('Tags', [])
                tags_json = json.dumps(tags) if tags else '[]'
                
                eip_name = 'N/A'
                for tag in tags:
                    if tag.get('Key') == 'Name':
                        eip_name = tag.get('Value', 'N/A')
                        break
                
                item_data = {
                    'allocationId': address.get('AllocationId'),
                    'eipName': eip_name,
                    'publicIp': address.get('PublicIp', 'N/A'),
                    'privateIpAddress': address.get('PrivateIpAddress'),
                    'instanceId': address.get('InstanceId'),
                    'networkInterfaceId': address.get('NetworkInterfaceId'),
                    'networkInterfaceOwnerId': address.get('NetworkInterfaceOwnerId'),
                    'associationId': address.get('AssociationId'),
                    'domain': address.get('Domain', 'standard'),
                    'networkBorderGroup': address.get('NetworkBorderGroup', 'N/A'),
                    'customerOwnedIp': address.get('CustomerOwnedIp'),
                    'customerOwnedIpv4Pool': address.get('CustomerOwnedIpv4Pool'),
                    'carrierIp': address.get('CarrierIp'),
                    'tags': tags_json
                }
                self.add_item('ElasticIP', eip_id, to_dynamodb_format(item_data))
                eip_count += 1
            
            if eip_count > 0:
                logger.debug(f"Added {eip_count} Elastic IPs from region {self.region} to the item list.")
            return eip_count
        except Exception as e:
            logger.error(f"Error collecting Elastic IPs in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_route_tables(self, ec2):
        """Collect Route Table information."""
        rt_count = 0
        try:
            response = ec2.describe_route_tables()
            for rt in response.get('RouteTables', []):
                rt_id = rt['RouteTableId']
                tags = rt.get('Tags', [])
                tags_json = json.dumps(tags) if tags else '[]'
                
                rt_name = 'N/A'
                for tag in tags:
                    if tag.get('Key') == 'Name':
                        rt_name = tag.get('Value', 'N/A')
                        break

                routes = rt.get('Routes', [])
                route_count = len(routes)
                internet_route = any(r.get('GatewayId', '').startswith('igw-') for r in routes)
                nat_route = any(r.get('NatGatewayId') for r in routes)
                vpc_peering_route = any(r.get('VpcPeeringConnectionId') for r in routes)
                
                associations = rt.get('Associations', [])
                associated_subnets = [a.get('SubnetId') for a in associations if a.get('SubnetId')]
                is_main = any(a.get('Main', False) for a in associations)
                
                item_data = {
                    'routeTableId': rt_id,
                    'routeTableName': rt_name,
                    'vpcId': rt.get('VpcId', 'N/A'),
                    'routeCount': route_count,
                    'hasInternetRoute': internet_route,
                    'hasNatRoute': nat_route,
                    'hasVpcPeeringRoute': vpc_peering_route,
                    'associationCount': len(associations),
                    'isMain': is_main,
                    'tags': tags_json,
                    'associatedSubnets': associated_subnets
                }
                self.add_item('RouteTable', rt_id, to_dynamodb_format(item_data))
                rt_count += 1
            
            if rt_count > 0:
                logger.debug(f"Added {rt_count} Route Tables from region {self.region} to the item list.")
            return rt_count
        except Exception as e:
            logger.error(f"Error collecting Route Tables in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_network_acls(self, ec2):
        """Collect Network ACL information."""
        nacl_count = 0
        try:
            response = ec2.describe_network_acls()
            for nacl in response.get('NetworkAcls', []):
                nacl_id = nacl['NetworkAclId']
                tags = nacl.get('Tags', [])
                tags_json = json.dumps(tags) if tags else '[]'
                
                nacl_name = 'N/A'
                for tag in tags:
                    if tag.get('Key') == 'Name':
                        nacl_name = tag.get('Value', 'N/A')
                        break

                entries = nacl.get('Entries', [])
                ingress_rules = [e for e in entries if not e.get('Egress', False)]
                egress_rules = [e for e in entries if e.get('Egress', False)]
                custom_deny_rules = [e for e in entries if e.get('RuleAction') == 'deny' and e.get('RuleNumber') != 32767]
                associations = nacl.get('Associations', [])
                associated_subnets = [a.get('SubnetId') for a in associations if a.get('SubnetId')]
                
                item_data = {
                    'networkAclId': nacl_id,
                    'networkAclName': nacl_name,
                    'vpcId': nacl.get('VpcId', 'N/A'),
                    'isDefault': nacl.get('IsDefault', False),
                    'ingressRuleCount': len(ingress_rules),
                    'egressRuleCount': len(egress_rules),
                    'customDenyRuleCount': len(custom_deny_rules),
                    'associationCount': len(associations),
                    'tags': tags_json,
                    'associatedSubnets': associated_subnets
                }
                self.add_item('NetworkACL', nacl_id, to_dynamodb_format(item_data))
                nacl_count += 1
            
            if nacl_count > 0:
                logger.debug(f"Added {nacl_count} Network ACLs from region {self.region} to the item list.")
            return nacl_count
        except Exception as e:
            logger.error(f"Error collecting Network ACLs in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_vpc_endpoints(self, ec2):
        """Collect VPC Endpoint information."""
        endpoint_count = 0
        try:
            paginator = ec2.get_paginator('describe_vpc_endpoints')
            for page in paginator.paginate():
                for endpoint in page.get('VpcEndpoints', []):
                    endpoint_id = endpoint['VpcEndpointId']
                    tags = endpoint.get('Tags', [])
                    tags_json = json.dumps(tags) if tags else '[]'

                    endpoint_name = next(
                        (t.get('Value') for t in tags if t.get('Key') == 'Name'),
                        'N/A'
                    )

                    creation_timestamp = endpoint.get('CreationTimestamp')
                    formatted_creation = format_aws_datetime(creation_timestamp) if creation_timestamp else None
                    route_table_ids = endpoint.get('RouteTableIds', [])
                    subnet_ids = endpoint.get('SubnetIds', [])
                    security_group_ids = [g['GroupId'] for g in endpoint.get('Groups', [])]

                    item_data = {
                        'vpcEndpointId': endpoint_id,
                        'vpcEndpointName': endpoint_name,
                        'vpcId': endpoint.get('VpcId', 'N/A'),
                        'serviceName': endpoint.get('ServiceName', 'N/A'),
                        'vpcEndpointType': endpoint.get('VpcEndpointType', 'N/A'),
                        'state': endpoint.get('State', 'N/A'),
                        'policyDocument': endpoint.get('PolicyDocument', '{}'),
                        'privateDnsEnabled': endpoint.get('PrivateDnsEnabled', False),
                        'createdAt': formatted_creation,
                        'tags': tags_json,
                        'routeTableIds': route_table_ids,
                        'subnetIds': subnet_ids,
                        'securityGroupIds': security_group_ids
                    }
                    self.add_item('VPCEndpoint', endpoint_id, to_dynamodb_format(item_data))
                    endpoint_count += 1

            if endpoint_count > 0:
                logger.debug(f"Added {endpoint_count} VPC Endpoints from region {self.region} to the item list.")
            return endpoint_count
        except Exception as e:
            logger.error(f"Error collecting VPC Endpoints in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_vpc_peering_connections(self, ec2):
        """Collect VPC Peering Connection information."""
        peering_count = 0
        try:
            response = ec2.describe_vpc_peering_connections()
            for peering in response.get('VpcPeeringConnections', []):
                peering_id = peering['VpcPeeringConnectionId']
                tags = peering.get('Tags', [])
                tags_json = json.dumps(tags) if tags else '[]'
                
                peering_name = 'N/A'
                for tag in tags:
                    if tag.get('Key') == 'Name':
                        peering_name = tag.get('Value', 'N/A')
                        break
                
                accepter_vpc = peering.get('AccepterVpcInfo', {})
                requester_vpc = peering.get('RequesterVpcInfo', {})
                
                item_data = {
                    'vpcPeeringConnectionId': peering_id,
                    'peeringConnectionName': peering_name,
                    'status': peering.get('Status', {}).get('Code', 'N/A'),
                    'statusMessage': peering.get('Status', {}).get('Message', ''),
                    'accepterVpcId': accepter_vpc.get('VpcId', 'N/A'),
                    'accepterRegion': accepter_vpc.get('Region', 'N/A'),
                    'accepterOwnerId': accepter_vpc.get('OwnerId', 'N/A'),
                    'requesterVpcId': requester_vpc.get('VpcId', 'N/A'),
                    'requesterRegion': requester_vpc.get('Region', 'N/A'),
                    'requesterOwnerId': requester_vpc.get('OwnerId', 'N/A'),
                    'tags': tags_json
                }
                self.add_item('VPCPeeringConnection', peering_id, to_dynamodb_format(item_data))
                peering_count += 1
            
            if peering_count > 0:
                logger.debug(f"Added {peering_count} VPC Peering Connections from region {self.region} to the item list.")
            return peering_count
        except Exception as e:
            logger.error(f"Error collecting VPC Peering Connections in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_vpn_connections(self, ec2):
        """Collect VPN Connection information."""
        vpn_count = 0
        try:
            response = ec2.describe_vpn_connections()
            for vpn in response.get('VpnConnections', []):
                vpn_id = vpn['VpnConnectionId']
                tags = vpn.get('Tags', [])
                tags_json = json.dumps(tags) if tags else '[]'
                
                vpn_name = 'N/A'
                for tag in tags:
                    if tag.get('Key') == 'Name':
                        vpn_name = tag.get('Value', 'N/A')
                        break

                vgw_telemetry = vpn.get('VgwTelemetry', [])
                tunnels_up = sum(1 for t in vgw_telemetry if t.get('Status') == 'UP')
                
                item_data = {
                    'vpnConnectionId': vpn_id,
                    'vpnConnectionName': vpn_name,
                    'state': vpn.get('State', 'N/A'),
                    'type': vpn.get('Type', 'N/A'),
                    'customerGatewayId': vpn.get('CustomerGatewayId', 'N/A'),
                    'vpnGatewayId': vpn.get('VpnGatewayId', 'N/A'),
                    'transitGatewayId': vpn.get('TransitGatewayId', 'N/A'),
                    'category': vpn.get('Category', 'N/A'),
                    'tunnelCount': len(vgw_telemetry),
                    'tunnelsUp': tunnels_up,
                    'tags': tags_json
                }
                self.add_item('VPNConnection', vpn_id, to_dynamodb_format(item_data))
                vpn_count += 1
            
            if vpn_count > 0:
                logger.debug(f"Added {vpn_count} VPN Connections from region {self.region} to the item list.")
            return vpn_count
        except Exception as e:
            logger.error(f"Error collecting VPN Connections in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_transit_gateways(self, ec2):
        """Collect Transit Gateway information."""
        tgw_count = 0
        try:
            paginator = ec2.get_paginator('describe_transit_gateways')
            for page in paginator.paginate():
                for tgw in page.get('TransitGateways', []):
                    tgw_id = tgw['TransitGatewayId']
                    tags = tgw.get('Tags', [])
                    tags_json = json.dumps(tags) if tags else '[]'
                    
                    tgw_name = 'N/A'
                    for tag in tags:
                        if tag.get('Key') == 'Name':
                            tgw_name = tag.get('Value', 'N/A')
                            break
                    
                    creation_time = tgw.get('CreationTime')
                    formatted_creation = format_aws_datetime(creation_time) if creation_time else None
                    
                    item_data = {
                        'transitGatewayId': tgw_id,
                        'transitGatewayName': tgw_name,
                        'state': tgw.get('State', 'N/A'),
                        'ownerId': tgw.get('OwnerId', 'N/A'),
                        'description': tgw.get('Description', ''),
                        'amazonSideAsn': tgw.get('Options', {}).get('AmazonSideAsn', 'N/A'),
                        'dnsSupport': tgw.get('Options', {}).get('DnsSupport', 'N/A'),
                        'vpnEcmpSupport': tgw.get('Options', {}).get('VpnEcmpSupport', 'N/A'),
                        'defaultRouteTableAssociation': tgw.get('Options', {}).get('DefaultRouteTableAssociation', 'N/A'),
                        'defaultRouteTablePropagation': tgw.get('Options', {}).get('DefaultRouteTablePropagation', 'N/A'),
                        'multicastSupport': tgw.get('Options', {}).get('MulticastSupport', 'N/A'),
                        'createdAt': formatted_creation,
                        'tags': tags_json
                    }
                    self.add_item('TransitGateway', tgw_id, to_dynamodb_format(item_data))
                    tgw_count += 1
            
            if tgw_count > 0:
                logger.debug(f"Added {tgw_count} Transit Gateways from region {self.region} to the item list.")
            return tgw_count
        except Exception as e:
            logger.error(f"Error collecting Transit Gateways in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_transit_gateway_attachments(self, ec2):
        """Collect Transit Gateway Attachment information."""
        tgw_attach_count = 0
        try:
            paginator = ec2.get_paginator('describe_transit_gateway_attachments')
            for page in paginator.paginate():
                for attachment in page.get('TransitGatewayAttachments', []):
                    attachment_id = attachment['TransitGatewayAttachmentId']
                    tags = attachment.get('Tags', [])
                    tags_json = json.dumps(tags) if tags else '[]'

                    attachment_name = next(
                        (t.get('Value') for t in tags if t.get('Key') == 'Name'),
                        'N/A'
                    )

                    creation_time = attachment.get('CreationTime')
                    formatted_creation = format_aws_datetime(creation_time) if creation_time else None
                    association = attachment.get('Association', {})

                    item_data = {
                        'transitGatewayAttachmentId': attachment_id,
                        'attachmentName': attachment_name,
                        'transitGatewayId': attachment.get('TransitGatewayId', 'N/A'),
                        'transitGatewayOwnerId': attachment.get('TransitGatewayOwnerId', 'N/A'),
                        'attachedResourceType': attachment.get('ResourceType', 'N/A'),
                        'attachedResourceId': attachment.get('ResourceId', 'N/A'),
                        'resourceOwnerId': attachment.get('ResourceOwnerId', 'N/A'),
                        'state': attachment.get('State', 'N/A'),
                        'createdAt': formatted_creation,
                        'tags': tags_json,
                        'association': association
                    }
                    self.add_item('TransitGatewayAttachment', attachment_id, to_dynamodb_format(item_data))
                    tgw_attach_count += 1

            if tgw_attach_count > 0:
                logger.debug(f"Added {tgw_attach_count} Transit Gateway Attachments from region {self.region} to the item list.")
            return tgw_attach_count
        except Exception as e:
            logger.error(f"Error collecting Transit Gateway Attachments in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_application_load_balancers(self, elbv2):
        """Collect Application and Network Load Balancer information."""
        alb_count = 0
        try:
            paginator = elbv2.get_paginator('describe_load_balancers')
            for page in paginator.paginate():
                for lb in page.get('LoadBalancers', []):
                    lb_arn = lb['LoadBalancerArn']
                    lb_name = lb['LoadBalancerName']
                    
                    # Get tags
                    tags = []
                    tags_json = '[]'
                    try:
                        tag_response = elbv2.describe_tags(ResourceArns=[lb_arn])
                        tag_descriptions = tag_response.get('TagDescriptions', [])
                        if tag_descriptions:
                            tags = tag_descriptions[0].get('Tags', [])
                            tags_json = json.dumps(tags) if tags else '[]'
                    except Exception as e:
                        logger.warning(f"Could not get tags for load balancer {lb_name}: {str(e)}")
                    
                    lb_name_tag = next(
                        (t.get('Value') for t in tags if t.get('Key') == 'Name'),
                        'N/A'
                    )

                    creation_time = lb.get('CreatedTime')
                    formatted_creation = format_aws_datetime(creation_time) if creation_time else None

                    # Get target groups
                    target_groups = []
                    try:
                        tg_response = elbv2.describe_target_groups(LoadBalancerArn=lb_arn)
                        target_groups = [tg['TargetGroupArn'] for tg in tg_response.get('TargetGroups', [])]
                    except Exception as e:
                        logger.warning(f"Could not get target groups for load balancer {lb_name}: {str(e)}")

                    # Use raw lists instead of flattening
                    availability_zones = [az['ZoneName'] for az in lb.get('AvailabilityZones', [])]
                    security_groups = lb.get('SecurityGroups', [])

                    item_data = {
                        'loadBalancerArn': lb_arn,
                        'loadBalancerName': lb_name,
                        'loadBalancerNameTag': lb_name_tag,
                        'dnsName': lb.get('DNSName', 'N/A'),
                        'canonicalHostedZoneId': lb.get('CanonicalHostedZoneId', 'N/A'),
                        'scheme': lb.get('Scheme', 'N/A'),
                        'state': lb.get('State', {}).get('Code', 'N/A'),
                        'type': lb.get('Type', 'N/A'),
                        'vpcId': lb.get('VpcId', 'N/A'),
                        'ipAddressType': lb.get('IpAddressType', 'N/A'),
                        'createdAt': formatted_creation,
                        'tags': tags_json,
                        'availabilityZones': availability_zones,
                        'securityGroups': security_groups,
                        'targetGroups': target_groups
                    }
                    self.add_item('LoadBalancer', lb_arn, to_dynamodb_format(item_data))
                    alb_count += 1
            
            if alb_count > 0:
                logger.debug(f"Added {alb_count} Load Balancers from region {self.region} to the item list.")
            return alb_count
        except Exception as e:
            logger.error(f"Error collecting Application/Network Load Balancers in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_classic_load_balancers(self, elb):
        """Collect Classic Load Balancer information."""
        clb_count = 0
        try:
            paginator = elb.get_paginator('describe_load_balancers')
            for page in paginator.paginate():
                for lb in page.get('LoadBalancerDescriptions', []):
                    lb_name = lb['LoadBalancerName']

                    tags = []
                    tags_json = '[]'
                    try:
                        tag_response = elb.describe_tags(LoadBalancerNames=[lb_name])
                        tag_descriptions = tag_response.get('TagDescriptions', [])
                        if tag_descriptions:
                            tags = tag_descriptions[0].get('Tags', [])
                            tags_json = json.dumps(tags) if tags else '[]'
                    except Exception as e:
                        logger.warning(f"Could not get tags for classic load balancer {lb_name}: {str(e)}")
                    
                    lb_name_tag = next(
                        (t.get('Value') for t in tags if t.get('Key') == 'Name'),
                        'N/A'
                    )
                    
                    creation_time = lb.get('CreatedTime')
                    formatted_creation = format_aws_datetime(creation_time) if creation_time else None
                    availability_zones = lb.get('AvailabilityZones', [])
                    subnets = lb.get('Subnets', [])
                    security_groups = lb.get('SecurityGroups', [])
                    instances = [i['InstanceId'] for i in lb.get('Instances', [])]
                    health_check = lb.get('HealthCheck', {})

                    item_data = {
                        'loadBalancerName': lb_name,
                        'loadBalancerNameTag': lb_name_tag,
                        'dnsName': lb.get('DNSName', 'N/A'),
                        'canonicalHostedZoneName': lb.get('CanonicalHostedZoneName', 'N/A'),
                        'canonicalHostedZoneNameId': lb.get('CanonicalHostedZoneNameID', 'N/A'),
                        'scheme': lb.get('Scheme', 'N/A'),
                        'vpcId': lb.get('VPCId', 'N/A'),
                        'instanceCount': len(lb.get('Instances', [])),
                        'createdAt': formatted_creation,
                        'tags': tags_json,
                        'availabilityZones': availability_zones,
                        'subnets': subnets,
                        'securityGroups': security_groups,
                        'instances': instances,
                        'healthCheck': health_check
                    }
                    self.add_item('ClassicLoadBalancer', lb_name, to_dynamodb_format(item_data))
                    clb_count += 1
            
            if clb_count > 0:
                logger.debug(f"Added {clb_count} Classic Load Balancers from region {self.region} to the item list.")
            return clb_count
        except Exception as e:
            logger.error(f"Error collecting Classic Load Balancers in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_auto_scaling_groups(self, autoscaling):
        """Collect Auto Scaling Group information."""
        asg_count = 0
        try:
            paginator = autoscaling.get_paginator('describe_auto_scaling_groups')
            for page in paginator.paginate():
                for asg in page.get('AutoScalingGroups', []):
                    asg_name = asg['AutoScalingGroupName']
                    tags = asg.get('Tags', [])
                    standard_tags = [{'Key': t.get('Key'), 'Value': t.get('Value')} for t in tags]
                    tags_json = json.dumps(standard_tags) if standard_tags else '[]'
                    
                    asg_name_tag = next(
                        (t.get('Value') for t in tags if t.get('Key') == 'Name'),
                        'N/A'
                    )
                    
                    creation_time = asg.get('CreatedTime')
                    formatted_creation = format_aws_datetime(creation_time) if creation_time else None
                    instances = asg.get('Instances', [])
                    instance_ids = [i['InstanceId'] for i in instances]
                    healthy_instances = sum(1 for i in instances if i.get('HealthStatus') == 'Healthy')
                    availability_zones = asg.get('AvailabilityZones', [])
                    load_balancer_names = asg.get('LoadBalancerNames', [])
                    target_group_arns = asg.get('TargetGroupARNs', [])
                    launch_template = asg.get('LaunchTemplate', {})

                    item_data = {
                        'autoScalingGroupName': asg_name,
                        'autoScalingGroupNameTag': asg_name_tag,
                        'autoScalingGroupARN': asg.get('AutoScalingGroupARN', 'N/A'),
                        'launchConfigurationName': asg.get('LaunchConfigurationName'),
                        'minSize': asg.get('MinSize', 0),
                        'maxSize': asg.get('MaxSize', 0),
                        'desiredCapacity': asg.get('DesiredCapacity', 0),
                        'currentSize': len(instances),
                        'healthyInstances': healthy_instances,
                        'healthCheckType': asg.get('HealthCheckType', 'N/A'),
                        'healthCheckGracePeriod': asg.get('HealthCheckGracePeriod', 0),
                        'vpcZoneIdentifier': asg.get('VPCZoneIdentifier', ''),
                        'serviceLinkedRoleARN': asg.get('ServiceLinkedRoleARN', 'N/A'),
                        'createdAt': formatted_creation,
                        'tags': tags_json,
                        'instanceIds': instance_ids,
                        'availabilityZones': availability_zones,
                        'loadBalancerNames': load_balancer_names,
                        'targetGroupARNs': target_group_arns,
                        'launchTemplate': launch_template
                    }
                    self.add_item('AutoScalingGroup', asg_name, to_dynamodb_format(item_data))
                    asg_count += 1
            
            if asg_count > 0:
                logger.debug(f"Added {asg_count} Auto Scaling Groups from region {self.region} to the item list.")
            return asg_count
        except Exception as e:
            logger.error(f"Error collecting Auto Scaling Groups in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_direct_connect_connections(self, directconnect):
        """Collect Direct Connect Connection information."""
        dx_count = 0
        try:
            response = directconnect.describe_connections()
            for conn in response.get('connections', []):
                conn_id = conn['connectionId']
                conn_name = conn.get('connectionName', 'N/A')
                
                # Direct Connect doesn't support tags in the same way
                tags = []
                tags_json = '[]'
                try:
                    tag_response = directconnect.describe_tags(resourceArns=[conn.get('connectionArn', '')])
                    tags = tag_response.get('resourceTags', [])[0].get('tags', []) if tag_response.get('resourceTags') else []
                    tags_json = json.dumps(tags) if tags else '[]'
                except Exception as e:
                    logger.debug(f"Could not get tags for Direct Connect connection {conn_id}: {str(e)}")
                
                item_data = {
                    'connectionId': conn_id,
                    'connectionName': conn_name,
                    'connectionState': conn.get('connectionState', 'N/A'),
                    'region': conn.get('region', self.region),
                    'location': conn.get('location', 'N/A'),
                    'bandwidth': conn.get('bandwidth', 'N/A'),
                    'vlan': conn.get('vlan', 0),
                    'partnerName': conn.get('partnerName', 'N/A'),
                    'loaIssueTime': format_aws_datetime(conn.get('loaIssueTime')) if conn.get('loaIssueTime') else None,
                    'lagId': conn.get('lagId'),
                    'awsDevice': conn.get('awsDevice', 'N/A'),
                    'awsDeviceV2': conn.get('awsDeviceV2', 'N/A'),
                    'hasLogicalRedundancy': conn.get('hasLogicalRedundancy', 'N/A'),
                    'macSecCapable': conn.get('macSecCapable', False),
                    'portEncryptionStatus': conn.get('portEncryptionStatus', 'N/A'),
                    'encryptionMode': conn.get('encryptionMode', 'N/A'),
                    'tags': tags_json
                }
                self.add_item('DirectConnectConnection', conn_id, to_dynamodb_format(item_data))
                dx_count += 1
            
            if dx_count > 0:
                logger.debug(f"Added {dx_count} Direct Connect Connections from region {self.region} to the item list.")
            return dx_count
        except Exception as e:
            logger.error(f"Error collecting Direct Connect Connections in region {self.region}: {str(e)}", exc_info=True)
            return 0
    
    def _collect_direct_connect_virtual_interfaces(self, directconnect):
        """Collect Direct Connect Virtual Interface information."""
        vif_count = 0
        try:
            response = directconnect.describe_virtual_interfaces()
            for vif in response.get('virtualInterfaces', []):
                vif_id = vif['virtualInterfaceId']
                vif_name = vif.get('virtualInterfaceName', 'N/A')

                # Use raw lists/objects instead of flattening
                route_filter_prefixes = vif.get('routeFilterPrefixes', [])
                bgp_peers = vif.get('bgpPeers', [])

                # ===== Novo: extrair status de BGP para atributos de topo =====
                def _status_of_family(peers, fam):
                    fam = (fam or "").lower()
                    for p in peers or []:
                        if (p.get('addressFamily') or "").lower() == fam:
                            return (p.get('bgpStatus') or 'unknown').lower()
                    return None

                status_ipv4 = _status_of_family(bgp_peers, 'ipv4')
                status_ipv6 = _status_of_family(bgp_peers, 'ipv6')

                # primário: prioriza IPv4, depois IPv6, senão o primeiro peer
                primary_status = (
                    status_ipv4
                    or status_ipv6
                    or ((bgp_peers[0].get('bgpStatus') or 'unknown').lower() if bgp_peers else 'unknown')
                )

                any_up = any((p.get('bgpStatus') or '').lower() == 'up' for p in (bgp_peers or []))
                all_up = bool(bgp_peers) and all((p.get('bgpStatus') or '').lower() == 'up' for p in bgp_peers)

                item_data = {
                    'virtualInterfaceId': vif_id,
                    'virtualInterfaceName': vif_name,
                    'connectionId': vif.get('connectionId', 'N/A'),
                    'virtualInterfaceType': vif.get('virtualInterfaceType', 'N/A'),
                    'virtualInterfaceState': vif.get('virtualInterfaceState', 'N/A'),
                    'customerAddress': vif.get('customerAddress', 'N/A'),
                    'amazonAddress': vif.get('amazonAddress', 'N/A'),
                    'vlan': vif.get('vlan', 0),
                    'asn': vif.get('asn', 0),
                    'amazonSideAsn': vif.get('amazonSideAsn', 0),
                    'authKey': 'REDACTED' if vif.get('authKey') else 'N/A',
                    'customerRouterConfig': 'Available' if vif.get('customerRouterConfig') else 'N/A',
                    'mtu': vif.get('mtu', 1500),
                    'jumboFrameCapable': vif.get('jumboFrameCapable', False),
                    'virtualGatewayId': vif.get('virtualGatewayId'),
                    'directConnectGatewayId': vif.get('directConnectGatewayId'),
                    'tags': json.dumps(vif.get('tags', [])) if vif.get('tags') else '[]',
                    'routeFilterPrefixes': route_filter_prefixes,
                    'bgpPeers': bgp_peers,

                    # ===== Novos campos "flat" para o front =====
                    'bgpStatus': primary_status,      # up/down/unknown (prioriza IPv4)
                    'bgpStatusIpv4': status_ipv4,     # ex.: up / down / None
                    'bgpStatusIpv6': status_ipv6,     # ex.: up / down / None
                    'bgpAnyUp': any_up,               # True se qualquer peer estiver up
                    'bgpAllUp': all_up,               # True se todos estiverem up
                }

                self.add_item('DirectConnectVirtualInterface', vif_id, to_dynamodb_format(item_data))
                vif_count += 1

            if vif_count > 0:
                logger.debug(f"Added {vif_count} Direct Connect Virtual Interfaces from region {self.region} to the item list.")
            return vif_count
        except Exception as e:
            logger.error(f"Error collecting Direct Connect Virtual Interfaces in region {self.region}: {str(e)}", exc_info=True)
            return 0