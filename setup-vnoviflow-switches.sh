#!/bin/bash

SCRIPT_NAME=$0
RETRY=0
MAXWAIT=600
SWITCHES=0
NOTIFY=""
TAG=""
ACTION=""
ASSUME_YES=""
ORIG_ARGS=()

SWITCHES_LIMIT=20
CREATED=""


function action_help(){
  test -n "$1" && echo "ERROR: $1"
  echo "USAGE: $SCRIPT_NAME [OPTIONS]"
  echo ""
  echo "  --switches NUMBER     Number of virtual Noviflow switches to be created."
  echo "  --max-wait TIME       Maximum time (seconds) to wait for services to be ready. Default: 600"
  echo "  --retry NUMBER        Number of retries after a failure is detected. Default: 0 (no retry)"
  echo "  --tag TEXT            Tag to be added to created switches. Useful for later manage resources."
  echo "  --notify EMAIL[,...]  E-mail address to be notified in case resources has to be removed or changed later."
  echo "  -y|--yes              Assume 'yes' as answer to all prompts and run non-interactively."
  echo "  -c|--create           Create switches"
  echo "  -d|--delete           Delete selected switches according to the tag (if tag is empty, delete all)"
  echo "  -l|--list             List created switches for a certain tag (if tag is empty, list all)"
  echo "  -h|--help             Show this help message and exit"
  exit 0
}

function get_available_switches(){
	EXISTING=$(vboxmanage list vms | grep vnovisw)
	for ID in $(seq 1 $SWITCHES_LIMIT); do
		if echo "$EXISTING" | grep -q -w "\"vnovisw$ID\""; then
			continue
		fi
		echo vnovisw$ID
	done
}

function create_switches(){
	AVAIL_SW=$(get_available_switches)
	X=$(echo "$AVAIL_SW" | wc -l)
	if [ $X -lt $SWITCHES ]; then
		echo "ERROR: not available switches: requested=$SWITCHES available=$X"
		exit 0
	fi
	TO_CREATE=$(echo "$AVAIL_SW" | head -n $SWITCHES)
	NOW=$(date +%s)
	for NAME in $TO_CREATE; do
		ID=$(echo $NAME | sed 's/vnovisw//')
		vboxmanage clonevm vNoviflow11 --name=$NAME --register --options=link --snapshot=NW570.6.1
		VBoxManage modifyvm $NAME --vrdemulticon on --vrdeport $((5000+$ID)) --description="X-VNOVI-CTL;tag=$TAG;notify=$NOTIFY;createdat=$NOW;"
		vboxmanage startvm --type headless $NAME
		CREATED="$CREATED $NAME"
	done
}

function check_switches_ready() {
	test $MAXWAIT -eq 0 && return 0
	OK=""
	COUNT=0
	START=$(date +%s)
	START_STR=$(date +%Y-%m-%d,%H:%M:%S)
	T=0
	echo -n "[$START_STR] Waiting switches to be ready (be patient, takes around 5min)..."
	while [ $T -lt $MAXWAIT ]; do
		for VM in $CREATED; do
			if echo "$OK" | grep -q -w $VM; then
				continue
			fi
			MAC=$(VBoxManage showvminfo $VM | egrep -o "MAC: [0-9a-fA-F]+" | awk '{print $2}' | sed 's/\(..\)/\1:/g; s/:$//')
			test -z "$MAC" && continue
			IP=$(VBoxManage dhcpserver findlease --network=HostInterfaceNetworking-vboxnet0 --mac-address=$MAC 2>/dev/null | grep "IP Address:" | awk '{print $NF}')
			test -z "$IP" && continue
			#ping -c4 -i 0.5 -w 4 $IP >/dev/null 2>&1
			#test $? -ne 0 && continue
			OUTPUT=$(timeout 5 sshpass -p "noviflow" ssh -o StrictHostKeyChecking=no superuser@$IP "show status port portno all" 2>&1)
			PORTS=$(echo "$OUTPUT" | egrep -c "up.*up")
			test $PORTS -ne 16 && continue
			OK="$OK $VM;$IP"
			COUNT=$(($COUNT+1))
		done
		if [ $COUNT -eq $SWITCHES ]; then
			echo " done!"
			return 0
		fi
		sleep 5
		T=$(($(date +%s) - $START))
		echo -n "."
	done
	echo " timeout!"
	return 1
}

function show_created_switches() {
	EXISTING=$(vboxmanage list vms | grep vnovisw | tr -d '"' | awk '{print $1}')
	for VM in $EXISTING; do
		MY_INFO=$(VBoxManage showvminfo $VM)
		DESC=$(echo "$MY_INFO" | grep -o "X-VNOVI-CTL;.*")
		MY_TAG=$(echo "$DESC" | egrep -o "tag=[^;]+" | cut -d'=' -f2)
		MY_NOTIFY=$(echo "$DESC" | egrep -o "notify=[^;]+" | cut -d'=' -f2)
		test -n "$TAG" -a "$TAG" != "$MY_TAG" && continue
		MAC=$(echo "$MY_INFO" | egrep -o "MAC: [0-9a-fA-F]+" | awk '{print $2}' | sed 's/\(..\)/\1:/g; s/:$//')
		if [ -z "$MAC" ] && ! echo "$@" | grep -q -- "--list-only"; then
			echo "ERROR: Found VM $VM but invalid MAC address"
			continue
		fi
		IP=$(VBoxManage dhcpserver findlease --network=HostInterfaceNetworking-vboxnet0 --mac-address=$MAC 2>/dev/null | grep "IP Address:" | awk '{print $NF}')
		if [ -z "$IP" ] && ! echo "$@" | grep -q -- "--list-only"; then
			echo "ERROR: Found VM $VM but invalid IP address"
			continue
		fi
		echo "$VM $IP"
	done
}

function action_create() {
	if ! echo $SWITCHES | egrep -q "^[0-9]+$" || [ $SWITCHES -lt 1 -o $SWITCHES -gt $SWITCHES_LIMIT ]; then
		echo "Error: invalid number of switches. Must be a number between 1 and $SWITCHES_LIMIT"
		echo ""
		action_help
	fi

	if ! ip link show dev vboxnet0 >/dev/null 2>&1; then
		VBoxManage hostonlyif create
	fi


	for I in $(seq 0 $RETRY); do
		#cleanup_existing_switches
		create_switches
		if check_switches_ready; then
			show_created_switches
			break
		else
			echo "Failed to create VMs"
		fi
	done
}

function ask_confirmation() {
	MSG=$1
	if [ "x$ASSUME_YES" != "xy" ]; then
		echo -n "$MSG"
		read RESP
		if [ "$RESP" != "y" ]; then
			return 1
		fi
	fi
	return 0
}

function action_delete() {
	VMS=$(show_created_switches --list-only)
	if [ -z "$VMS" ]; then
		echo "No VM found"
		return 0
	fi
	echo "$VMS"
	echo ""
	if ! ask_confirmation "Remove listed VMs? (y/N) "; then
		return 0
	fi
	for VM in $(echo "$VMS" | awk '{print $1}'); do
		echo "Removing VM $VM..."
		vboxmanage controlvm $VM poweroff
		vboxmanage unregistervm $VM --delete-all
	done
}

#######
## Main
#######

while [[ $# -gt 0 ]]; do
  case $1 in
    --max-wait)
      test -z "$2" && action_help "missing argument for $1"
      MAXWAIT=$2
      shift
      shift
      ;;
    --retry)
      test -z "$2" && action_help "missing argument for $1"
      RETRY=$2
      shift
      shift
      ;;
    --switches)
      test -z "$2" && action_help "missing argument for $1"
      SWITCHES=$2
      shift
      shift
      ;;
    --notify)
      NOTIFY=$(echo "$2" | tr ' ;\n' '_')
      test -z "$NOTIFY" && action_help "invalid argument for $1"
      shift
      shift
      ;;
    --tag)
      TAG=$(echo "$2" | tr ' ;\n' '_')
      test -z "$TAG" && action_help "invalid argument for $1"
      shift
      shift
      ;;
    -y|--yes)
      ASSUME_YES=y
      shift
      ;;
    -h|--help)
      ACTION=help
      shift
      ;;
    -l|--list)
      ACTION=list
      shift
      ;;
    -c|--create)
      ACTION=create
      shift
      ;;
    -d|--delete)
      ACTION=delete
      shift
      ;;
    *)
      action_help "Unknown option provided $1"
      exit 0
      #ORIG_ARGS+=("$1")
      #shift # past argument
      ;;
  esac
done


# additional args: ${ORIG_ARGS[@]}

if [ "$ACTION" == "create" ]; then
	action_create
elif [ "$ACTION" == "list" ]; then
	show_created_switches
elif [ "$ACTION" == "delete" ]; then
	action_delete
else
	action_help
fi
