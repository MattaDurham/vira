"""Modeled carp facial surfaces, including an articulated oral vestibule.

Jaw motion is informed by Gidmark et al.'s common-carp XROMM study:
https://xromm.org/projects/fish-feeding/ . This is an authored approximation,
not a specimen reconstruction. The respiratory cycle uses modest jaw motion,
not maximum feeding protrusion.
"""
import math
import random


def author_face(mesh,curve,sphere,material,surface,skin_uv,ivory,cornea,pupil):
    from mathutils import Vector
    rng=random.Random(418)
    flesh=material("Facial mucosa",(.65,.59,.50),.44)
    flesh.node_tree.nodes["Principled BSDF"].inputs["Subsurface Weight"].default_value=.16
    flesh.node_tree.nodes["Principled BSDF"].inputs["IOR"].default_value=1.04
    flesh.node_tree.nodes["Principled BSDF"].inputs["Coat Weight"].default_value=.08
    inner=material("Oral mucosa",(.18,.063,.055),.46)
    dark=material("Recessed sensory canals",(.018,.020,.015),.54)
    iris_mats=[material(f"Iris stromal {i}",(v*1.13,v*.87,v*.46),.38) for i,v in enumerate([.22,.30,.37,.43,.48,.54])]
    for mat in iris_mats:
        mat.node_tree.nodes["Principled BSDF"].inputs["IOR"].default_value=1.04
        mat.node_tree.nodes["Principled BSDF"].inputs["Coat Weight"].default_value=.04

    source=ivory.node_tree.nodes.get("Image Texture").image
    for mat in iris_mats:
        node=mat.node_tree.nodes.new("ShaderNodeTexImage");node.image=source
        mat.node_tree.links.new(node.outputs["Color"],mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])

    def patch(name,rows,columns,point,mat,reverse=False):
        vertices=[point(i/rows,j/columns) for i in range(rows+1) for j in range(columns+1)]
        faces=[]
        for i in range(rows):
            for j in range(columns):
                a=i*(columns+1)+j;f=(a,a+1,a+columns+2,a+columns+1)
                faces.append(tuple(reversed(f)) if reverse else f)
        return mesh(name,vertices,faces,[mat])

    for sign in [-1,1]:
        # An orbital annulus blends into the skull, rather than adding a ball
        # on top of the face. The iris sits inside this fleshy socket.
        cx,cz=-.534,.0035
        def socket(r,t):
            a=math.tau*t;radius=.018+.009*r
            x=cx+radius*math.cos(a);z=cz+radius*math.sin(a)
            width,_,_,center=_profile(surface,x)
            base=surface(x,math.asin(max(-.98,min(.98,(z-center)/max(width/.86,.001)))))
            y=abs(base.y)+.003*(1-r)**2
            return (x,sign*y,z)
        orbital=patch("Orbital dermal annulus",12,128,socket,ivory,sign<0);skin_uv(orbital)
        # Radial iris crypts and corrugated stromal fibres are actual geometry.
        vertices,faces,indices=[],[],[]
        rays,rings=192,20
        for i in range(rings+1):
            r=.0092+(.019-.0092)*i/rings
            for j in range(rays):
                a=math.tau*j/rays+.0016*math.sin(j*3.71)+.0011*math.sin(j*1.27)
                relief=.000018*math.sin(a*83+i*.7)+.000008*math.sin(a*137-i*.5)
                y=.084+.30*r*math.cos(a)+.0016*(1-(r/.022)**2)+relief
                vertices.append((cx+r*math.cos(a),sign*y,cz+r*math.sin(a)))
        for i in range(rings):
            for j in range(rays):
                a=i*rays+j;b=i*rays+(j+1)%rays
                f=(a,b,b+rays,a+rays);faces.append(f if sign>0 else tuple(reversed(f)))
                indices.append(max(0,min(5,int(2.6+1.3*math.sin(j*1.73)+rng.uniform(-.8,.8)))))
        stroma=mesh("Iris radial stroma",vertices,faces,iris_mats,indices);skin_uv(stroma)
        stroma["facial_joint"]="eye_L" if sign>0 else "eye_R"
        pupil_mesh=sphere("Recessed pupil",(cx,sign*.086,cz),(.0093,.0014,.0093),pupil,segments=64)
        pupil_mesh.rotation_euler.z=sign*math.atan(.30)
        pupil_mesh["facial_joint"]=stroma["facial_joint"]
        # Cornea is a shallow transparent dome, not a thick glass bead.
        def dome(r,t):
            radius=.0195*r;a=math.tau*t
            return (cx+radius*math.cos(a),sign*(.084+.30*radius*math.cos(a)+.0046*math.sqrt(max(0,1-r*r))),cz+radius*math.sin(a))
        lens=patch("Cornea",32,128,dome,cornea,sign<0)
        lens["facial_joint"]=stroma["facial_joint"]

        # Two openings connected by an olfactory canal, with a dividing flap.
        # Each rim follows the head surface and encloses a genuinely recessed cup.
        for x,angle,w,h in [(-.590,.43,.0042,.0027),(-.579,.48,.0035,.0030)]:
            anchor=surface(x,angle if sign>0 else math.pi-angle,.0006)
            normal=Vector((0,sign*math.cos(angle),math.sin(angle)))
            vertical=Vector((0,-sign*math.sin(angle),math.cos(angle)))
            def canal(r,t):
                a=math.tau*t
                return anchor+Vector((w*r*math.cos(a),0,0))+vertical*(h*r*math.sin(a))-normal*(.004*(1-r)**2)
            patch("Olfactory canal",12,64,canal,dark,sign<0)
            points=[]
            for j in range(65):
                a=math.tau*j/64
                points.append(anchor+Vector((w*math.cos(a),0,0))+vertical*(h*math.sin(a))+normal*.0005)
            rim=curve("Narial fleshy rim",points,.00065,ivory);skin_uv(rim)
        anchor=surface(-.585,.47 if sign>0 else math.pi-.47,.0012)
        flap=patch("Olfactory valve",12,20,lambda u,v:anchor+Vector((.004*(u-.5),sign*.002*math.sin(math.pi*u)*math.sin(math.pi*v),.004*(v-.5))),ivory,sign<0)
        skin_uv(flap)

        # The free posterior opercular edge has thickness and an inner membrane.
        def platepoint(t,u,inner_surface=False):
            angle=(-.95+1.25*t) if sign>0 else (math.pi+.95-1.25*t)
            rear=-.374-.039*abs(2*t-1)
            return surface(-.505+(rear+.505)*u,angle,(-.001 if inner_surface else .0006)+.002*math.sin(math.pi*u)*math.sin(math.pi*t))
        plate=patch("Opercular plate",40,40,platepoint,ivory,sign>0);skin_uv(plate);plate["facial_joint"]=f"operculum_{'L' if sign>0 else 'R'}"
        seal=patch("Branchiostegal seal",40,10,lambda t,u:platepoint(t,.82+.18*u,True),inner,sign<0)
        seal["facial_joint"]=plate["facial_joint"]
        edge=curve("Opercular free rim",[platepoint(i/80,1) for i in range(81)],.0007,ivory)
        edge["facial_joint"]=plate["facial_joint"];skin_uv(edge)

    for lower in [False,True]:
        start=math.pi if lower else 0
        def collar(t,u):
            a=start+math.pi*t
            return (-.660-.012*u,(.0245-.0005*u)*math.cos(a),(-.03335-.00965*u)+(.025-.0175*u+.0003*math.sin(math.pi*u))*math.sin(a))
        rostrum=patch("Rostral lip attachment",64,24,collar,ivory,False)
        rostrum["facial_joint"]="jaw_lower" if lower else "jaw_upper"
        skin_uv(rostrum)

    # Thick lips are modeled around a continuous oral tube. No opaque nose cap
    # covers this opening; the dark cavity is behind an actual aperture.
    for lower in [False,True]:
        joint="jaw_lower" if lower else "jaw_upper"
        start=math.pi if lower else 0
        def lips(t,u):
            a=start+math.pi*t;b=math.tau*u
            thickness=.0035*(.75+.25*math.sin(math.pi*t))
            wrinkle=.00010*math.sin(a*37+b*3)
            r=thickness+wrinkle
            return (-.673-r*math.cos(b),(.0215+r*math.sin(b))*math.cos(a),-.043+(.0035+r*math.sin(b))*math.sin(a))
        lip=patch("Lower labial fold" if lower else "Upper labial fold",96,24,lips,flesh,True)
        lip["facial_joint"]=joint
        def tube(t,u):
            a=start+math.pi*t
            w=.0195*(1-.64*u);h=.0025+.005*u
            return (-.673+.047*u,w*math.cos(a),-.043+h*math.sin(a))
        oral=patch("Lower buccal lining" if lower else "Upper buccal lining",96,24,tube,inner,lower)
        oral["facial_joint"]=joint
        for fold in range(3):
            # Fine longitudinal folds are sub-millimetre relief, not painted lines.
            points=[tube(i/48,.12+.19*fold) for i in range(49)]
            fold_obj=curve("Oral mucosal fold",points,.00038,inner);fold_obj["facial_joint"]=joint
    sphere("Buccal depth",(-.624,0,-.043),(.0025,.009,.006),dark,segments=48)

    # Two tapered sensory pairs. Cross-section shrinks to a soft rounded tip.
    for sign in [-1,1]:
        for small in [False,True]:
            origin=Vector((-.626,sign*.034,-.027)) if small else Vector((-.651,sign*.024,-.044))
            end=Vector((-.669,sign*.058,-.045)) if small else Vector((-.706,sign*.061,-.083))
            vertices,faces=[],[]
            for i in range(49):
                t=i/48;point=origin.lerp(end,t)
                point.z+=.007*math.sin(math.pi*t);point.y+=sign*.008*math.sin(math.pi*t)
                radius=(.00135 if small else .0022)*(1-t)**.72+.00012
                for j in range(16):
                    a=math.tau*j/16;vertices.append(point+Vector((0,radius*math.cos(a),radius*math.sin(a))))
            for i in range(48):
                for j in range(16):
                    a=i*16+j;b=i*16+(j+1)%16;faces.append((a,b,b+16,a+16))
            barbel=mesh("Rostral sensory barbel" if small else "Maxillary sensory barbel",vertices,faces,[flesh])
            barbel["facial_joint"]="jaw_upper"


def _profile(surface,x):
    # Recover the envelope without coupling this module to the profile table.
    a=surface(x,0);b=surface(x,math.pi/2);c=surface(x,-math.pi/2)
    return abs(a.y),b.z-a.z,a.z-c.z,a.z
