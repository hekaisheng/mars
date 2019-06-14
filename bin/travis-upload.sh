#!/bin/bash
set -e -x

if [ -z "$TRAVIS_TAG" ]; then
  echo "Not on a tag, won't deploy to pypi"
else
  git clean -f -x

  if [ "$TRAVIS_OS_NAME" = "linux" ]; then
    sudo chmod 777 bin/*
    docker pull $DOCKER_IMAGE
    docker run --rm -e "PYVER=$PYVER" -v `pwd`:/io $DOCKER_IMAGE $PRE_CMD /io/bin/travis-build-wheels.sh

    if [ "$PYVER2" ]; then
      docker run --rm -e "PYVER=$PYVER2" -v `pwd`:/io $DOCKER_IMAGE $PRE_CMD /io/bin/travis-build-wheels.sh
    fi

  else
    virtualenv wheelenv
    source wheelenv/bin/activate

    pip install -r requirements-wheel.txt
    pip wheel --no-deps .

    if [ -z "$DEFAULT_VENV" ]; then
      deactivate
    else
      source $DEFAULT_VENV/bin/activate
    fi
    mkdir dist
    cp *.whl dist/
    pip install delocate
    delocate-wheel dist/*.whl
    delocate-addplat --rm-orig -x 10_9 -x 10_10 dist/*.whl
  fi
  ls dist/

  echo "[distutils]"                                  > ~/.pypirc
  echo "index-servers ="                             >> ~/.pypirc
  echo "    pypi"                                    >> ~/.pypirc
  echo "    testpypi"                                    >> ~/.pypirc
  echo "[testpypi]"                                      >> ~/.pypirc
  echo "repository=https://test.pypi.org/legacy/"  >> ~/.pypirc
  echo "username=pyodps"                             >> ~/.pypirc
  echo "password=$PYPIPSD"                            >> ~/.pypirc

  python -m pip install twine
  python -m twine upload -r testpypi --skip-existing dist/*.whl
fi
