
set -x

if [[ -z $1 ]]; then echo please give the agrument for user; exit 1; fi

USER=$1
PROJECT=${USER}_dev

password=$(echo $RANDOM | sha1sum | head -c 16)

(openstack user show $USER && openstack user set --password $password $USER) || openstack user create --password $password $USER
openstack project show $PROJECT || openstack project create $PROJECT

# Data services use explicit project-scoped roles rather than the broad
# OpenStack member role. Creation is idempotent for existing installations.
for role in data_reader data_editor data_admin; do
  openstack role show "$role" >/dev/null 2>&1 || openstack role create "$role"
done

openstack role add --user $USER --project $PROJECT load-balancer_observer
openstack role add --user $USER --project $PROJECT member
openstack role add --user $USER --project $PROJECT load-balancer_member
openstack role add --user $USER --project $PROJECT heat_stack_user
openstack role add --user $USER --project $PROJECT data_editor

cat <<EOF
------

user: $USER
password: $password

$(openstack endpoint list -f value -c URL | grep -o http.*: | sort | uniq | head -n 1 | xargs -I% echo %9999)

EOF
