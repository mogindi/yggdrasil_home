

modprobe nbd max_part=8

run_linux_cmd_in_qcow2_image () {

	image_path=$1
	cmd=$2
	mnt_args=$3

	mount_dir=/mnt/mod-image-$RANDOM
    nbd_dev_num=$(lsblk -d | grep nbd[0-9] | grep ' 0B ' | wc -l)
	nbd_dev="/dev/$(lsblk -d | grep nbd[0-9] | grep ' 0B ' | awk '{print $1}' | sed "$(( $RANDOM % $nbd_dev_num ))q;d")"
	# seems set -e ignored inside the sub-shell (not sure why), adding "|| exit 1 " in each line instead
	if ! [[ -z $cmd ]]; then
		(
			set -x
			mkdir -p $mount_dir
			qemu-nbd --connect=$nbd_dev $image_path
			sleep 1
			lsblk -d ${nbd_dev}p* -o NAME,SIZE | sort -k 2 -h | tail -n 1 | awk '{print $1}' | xargs -I% mount $mnt_args /dev/% $mount_dir  || exit 1 
			sleep 1
			chroot $mount_dir /bin/bash -c "mv /etc/resolv.conf /etc/resolv.conf.bk && echo nameserver 8.8.8.8 | tee /etc/resolv.conf" || exit 1
			echo ==== RUNNING CMD ====
			chroot $mount_dir /bin/bash -c "set -x; $cmd" || exit 1
			echo ==== END CMD ====
			chroot $mount_dir /bin/bash -c "mv /etc/resolv.conf.bk /etc/resolv.conf" || exit 1

			umount $mount_dir
			qemu-nbd --disconnect $nbd_dev
			sleep 2
			rm -rf $mount_dir
		) || \
		(
			set -x
			umount $mount_dir
			qemu-nbd --disconnect $nbd_dev
			sleep 2
			rm -rf $mount_dir
			exit 1
		)
	fi

}

copy_files_to_qcow2_image () {
	image_path=$1
	source_dir=$2
	target_dir=$3
	mnt_args=$4

	if ! [[ -d $source_dir ]]; then
		echo "Injection source directory does not exist: $source_dir" >&2
		return 1
	fi

	mount_dir=$(mktemp -d /mnt/mod-image-XXXXXX)
	nbd_dev=$(lsblk -d -o NAME,SIZE | awk '$2 == "0B" {print "/dev/" $1; exit}')
	if [[ -z $nbd_dev ]]; then
		echo "No free NBD device is available for image injection" >&2
		rmdir "$mount_dir"
		return 1
	fi

	cleanup_image_mount() {
		umount "$mount_dir" >/dev/null 2>&1 || true
		qemu-nbd --disconnect "$nbd_dev" >/dev/null 2>&1 || true
		rm -rf "$mount_dir" || true
	}

	if ! qemu-nbd --connect="$nbd_dev" "$image_path"; then
		cleanup_image_mount
		return 1
	fi
	sleep 1

	partition=$(lsblk -ln -o PATH,TYPE "$nbd_dev" | awk '$2 == "part" {print $1; exit}')
	partition=${partition:-$nbd_dev}
	if ! mount $mnt_args "$partition" "$mount_dir"; then
		cleanup_image_mount
		return 1
	fi

	if ! mkdir -p "$mount_dir/$target_dir"; then
		cleanup_image_mount
		return 1
	fi
	if ! cp -a "$source_dir"/. "$mount_dir/$target_dir"/; then
		cleanup_image_mount
		return 1
	fi
	find "$mount_dir/$target_dir" -type f -name '*.pyc' -delete
	find "$mount_dir/$target_dir" -depth -type d -name __pycache__ -delete

	sync
	cleanup_image_mount
}

create_openstack_linux_image() {
	image_url=$1
	image_name=$2
	cmd=$3
	extra_args=$4
	xz_decompress=$5
	mount_args=$6
	injection_source_dir=$7
	injection_target_dir=$8


	image_format=qcow2

	openstack image list -f value -c Name | grep $(echo $image_name | sed 's/\..*//g') || {
		echo ================= $image_name =================
		
		rm -f $image_name*
		if [[ -z $xz_decompress ]]; then
			wget -q $image_url -O $image_name.$image_format

		else
			wget -q $image_url -O $image_name.$image_format.xz
			xz -d $image_name.$image_format.xz
		fi
		run_linux_cmd_in_qcow2_image  $image_name.$image_format "$cmd" "$mount_args"
		if [[ -n $injection_source_dir ]]; then
			copy_files_to_qcow2_image "$image_name.$image_format" \
				"$injection_source_dir" "$injection_target_dir" "$mount_args"
		fi
		qemu-img convert $image_name.$image_format $image_name.raw
		openstack image create $image_name --file $image_name.raw $extra_args
		rm -f $image_name*
	} 2>&1  # | tail -n 99999  # adding this to output all at once
}


image_cleanup() {
  pkill -f qemu-nbd || true
  rm -rf *.qcow2*
  rm -rf /mnt/mod-image-*
}
