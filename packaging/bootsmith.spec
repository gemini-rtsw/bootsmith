# RPM for Bootsmith.
#
# Ships the systemd unit, its host settings and the profiles repo -- NOT the
# application, which runs from the container image on GHCR. The unit is pinned
# to the image built from the same commit, so `rpm -q bootsmith` names exactly
# what runs and `dnf downgrade` is a real rollback.
#
# THE VERSION LIVES HERE. Bump specver; the image tag follows it.
%global specver 0.1.0

# $GIT_HASH first: build_rpm.sh computes it on the host and passes it into the
# build container. git alone can resolve to "nogit" there, and the unit would
# then pin an image tag that was never pushed.
%define git_hash %(if [ -n "$GIT_HASH" ]; then echo "$GIT_HASH"; else git rev-parse --short HEAD 2>/dev/null || echo nogit; fi)
%global appimage ghcr.io/gemini-rtsw/bootsmith

# profiles/ is a git submodule of bootsmith-config. build_rpm.sh runs rpmbuild
# from the checkout, which the source tarball does not carry .git for, so the
# repo history is read from there.
%global profiles_repo %(pwd)/profiles

Name:           bootsmith
Version:        %{specver}
Release:        3.git%{git_hash}%{?dist}
Summary:        Bootsmith: VME crate boot console (runs as a container)

License:        Proprietary
URL:            https://github.com/gemini-rtsw/bootsmith
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  systemd-rpm-macros
BuildRequires:  git
Requires:       systemd
Requires:       git
Requires(pre):  shadow-utils

%description
Web console for setting VME crate boot parameters over WTI terminal servers.

This package contains only the systemd unit, its settings and the
profiles repo. The application runs from the container image
%{appimage}:%{version}-git%{git_hash}, which the unit pulls on start.

    systemctl enable --now bootsmith
    curl -s localhost:8080/

On first install /var/lib/bootsmith/profiles becomes a shallow clone of the
bootsmith-config repo (its main branch at one commit), unpacked from this
package, so no network is needed. Edit and verify a profile there, then git
commit and git push. git fetch --unshallow fetches the full history.

%prep
%autosetup

%build
# Pin the NVR-unique tag, not the bare version: :<version> is retagged by every
# build of that version, which would make `dnf downgrade` a no-op for the image.
sed -e 's|@IMAGE@|%{appimage}:%{version}-git%{git_hash}|' \
    deploy/bootsmith.service.in > bootsmith.service
if grep -q '@IMAGE@' bootsmith.service; then
    echo "ERROR: image placeholder not substituted" >&2; exit 1
fi
# A shallow clone of the profiles repo: just the pinned commit, on main,
# tracking origin/main. Shipped as a tarball, so its size stays small however
# much history the repo grows.
rm -rf profiles-repo
git -c safe.directory='*' clone -q --depth 1 --no-local "file://%{profiles_repo}" profiles-repo
git -C profiles-repo checkout -q -B main
git -C profiles-repo update-ref refs/remotes/origin/main HEAD
git -C profiles-repo branch -q --set-upstream-to=origin/main main
tar -C profiles-repo -czf profiles-repo.tar.gz .

%install
install -Dpm 0644 bootsmith.service        %{buildroot}%{_unitdir}/bootsmith.service
install -Dpm 0644 deploy/bootsmith.sysconfig %{buildroot}%{_sysconfdir}/sysconfig/bootsmith
install -dm 0755 %{buildroot}%{_sharedstatedir}/bootsmith/profiles
install -Dpm 0644 profiles-repo.tar.gz       %{buildroot}%{_datadir}/bootsmith/profiles-repo.tar.gz

%pre
getent group bootsmith >/dev/null || groupadd -r bootsmith
getent passwd bootsmith >/dev/null || \
    useradd -r -g bootsmith -d %{_sharedstatedir}/bootsmith -s /sbin/nologin \
            -c "Bootsmith service" bootsmith
exit 0

%post
%systemd_post bootsmith.service
# Operators commit in a directory owned by other users; tell git that is fine.
git config --system --get-all safe.directory 2>/dev/null | grep -qx '%{_sharedstatedir}/bootsmith/profiles' || \
    git config --system --add safe.directory '%{_sharedstatedir}/bootsmith/profiles'

%posttrans
# First install only: unpack the shallow clone of bootsmith-config shipped in
# this package into the profiles directory. Runs at the end of the transaction because
# an upgrade from 0.1.0-1/-2 deletes that release's profile files after the
# post-install script. Never touches a directory that is already a repo.
d=%{_sharedstatedir}/bootsmith/profiles
if [ ! -d "$d/.git" ]; then
    BOOTSMITH_PROFILES_REMOTE=git@github.com:gemini-rtsw/bootsmith-config.git
    [ -r %{_sysconfdir}/sysconfig/bootsmith ] && . %{_sysconfdir}/sysconfig/bootsmith
    if [ -n "$(ls -A "$d" 2>/dev/null)" ]; then
        old="$d.pre-git-$(date +%%Y%%m%%d%%H%%M%%S)"
        mkdir -p "$old" && mv "$d"/* "$d"/.[!.]* "$old"/ 2>/dev/null || :
        echo "bootsmith: moved existing profiles to $old" >&2
    fi
    tar -xzf %{_datadir}/bootsmith/profiles-repo.tar.gz -C "$d"
    git -C "$d" remote set-url origin "$BOOTSMITH_PROFILES_REMOTE"
    git -C "$d" config core.sharedRepository world
    chmod -R a+rwX "$d"
fi

%preun
%systemd_preun bootsmith.service

%postun
# No restart on upgrade: someone may be mid-way through booting a crate. The
# new image takes effect on the next `systemctl restart bootsmith`.
%systemd_postun bootsmith.service

%files
# Not a config file: the unit carries the image tag, so upgrades must replace it.
%{_unitdir}/bootsmith.service
%config(noreplace) %{_sysconfdir}/sysconfig/bootsmith
%dir %attr(0755,bootsmith,bootsmith) %{_sharedstatedir}/bootsmith
# World-writable so operators can edit and commit without root. The app saves
# by writing a temp file and renaming it, so it can update any profile in here,
# whoever owns the file. The contents are a git clone and are NOT owned by this
# package, so upgrades never touch them.
%dir %attr(0777,bootsmith,bootsmith) %{_sharedstatedir}/bootsmith/profiles
%{_datadir}/bootsmith/profiles-repo.tar.gz

%changelog
* Tue Oct 06 2026 Hawi Stecher <hawi.stecher@noirlab.edu> - 0.1.0-3
- On first install, make the profiles directory a shallow clone of
  bootsmith-config shipped in this package; profiles are no longer
  package-owned files.

* Tue Oct 06 2026 Hawi Stecher <hawi.stecher@noirlab.edu> - 0.1.0-2
- Make /var/lib/bootsmith/profiles writable by all users.

* Tue Oct 06 2026 Hawi Stecher <hawi.stecher@noirlab.edu> - 0.1.0-1
- Package for the gemini-rtsw-ci pipeline: systemd unit pinned to the image.
